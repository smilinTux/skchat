#!/usr/bin/env python3
"""jarvis-heartbeat.py — Agent polling loop for SKChat.

Polls skchat inbox for messages addressed to Jarvis or Opus, spawns `claude`
sessions inside tmux windows to handle them, and cleans up when done.

Each message gets its own tmux window inside the `skchat-agents` session:
  - Window name: {agent}-{short_id} (e.g., jarvis-a1b2c3, opus-d4e5f6)
  - SKCAPSTONE_AGENT set per-agent (jarvis or opus)
  - Window auto-closes when claude exits
  - Max concurrent windows enforced

Run as:
  python3 scripts/jarvis-heartbeat.py
  systemctl --user start jarvis-heartbeat.service
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# ─── Logging ──────────────────────────────────────────────────────────────────

LOG_DIR = Path.home() / ".skchat"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "jarvis-heartbeat.log"
RESPONSE_LOG = LOG_DIR / "jarvis-responses.log"

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s")

_file_handler = logging.FileHandler(LOG_FILE)
_file_handler.setFormatter(_fmt)

_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_fmt)

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stream_handler])
logger = logging.getLogger("jarvis-heartbeat")

# ─── Agent identities ────────────────────────────────────────────────────────

AGENTS = {
    "jarvis": {
        "identity": "capauth:jarvis@skworld.io",
        "variants": {
            "capauth:jarvis@skworld.io",
            "jarvis",
            "capauth:jarvis@capauth.local",
            "jarvis@skworld.io",
        },
        "mention": "@jarvis",
    },
    "opus": {
        "identity": "capauth:opus@skworld.io",
        "variants": {
            "capauth:opus@skworld.io",
            "opus",
            "capauth:opus@capauth.local",
            "opus@skworld.io",
        },
        "mention": "@opus",
    },
    "lumina": {
        "identity": "capauth:lumina@skworld.io",
        "variants": {
            "capauth:lumina@skworld.io",
            "lumina",
            "capauth:lumina@capauth.local",
            "lumina@skworld.io",
            "capauth:cbd2dot11@capauth.local",
        },
        "mention": "@lumina",
    },
}

CHEF_IDENTITY = "chef@skworld.io"
LUMINA_IDENTITY = "capauth:lumina@skworld.io"

# ─── Config ───────────────────────────────────────────────────────────────────

POLL_INTERVAL = int(os.environ.get("HEARTBEAT_POLL_INTERVAL", "10"))
RATE_LIMIT_SECONDS = 15
CLAUDE_TIMEOUT = 300  # 5 min per session
MAX_CONCURRENT = 3  # max tmux windows at once
TMUX_SESSION = "skchat-agents"

INBOX_PATH = Path.home() / ".skcomm" / "inbox"
FILE_TRANSPORT_INBOX = Path.home() / ".skcomm" / "transport" / "file" / "inbox"

CLAUDE_BIN = Path.home() / ".npm-global" / "bin" / "claude"
SKENV_BIN = Path.home() / ".skenv" / "bin"

_PROCESSED_FILE = Path.home() / ".skchat" / "heartbeat-processed.json"
_ACTIVE_SESSIONS: dict[str, str] = {}  # msg_key → tmux window name
_active_lock = threading.Lock()

# ─── Deduplication ────────────────────────────────────────────────────────────


def _load_processed() -> set[str]:
    try:
        if _PROCESSED_FILE.exists():
            return set(json.loads(_PROCESSED_FILE.read_text()))
    except Exception:
        pass
    return set()


def _save_processed(ids: set[str]) -> None:
    try:
        _PROCESSED_FILE.write_text(json.dumps(list(ids)[-500:]))
    except Exception as exc:
        logger.warning("Failed to save processed IDs: %s", exc)


PROCESSED: set[str] = _load_processed()
_last_response: dict[str, float] = {}

# ─── Metrics ──────────────────────────────────────────────────────────────────

METRICS_PORT = 9387

_METRICS: dict = {
    "messages_processed": 0,
    "responses_sent": 0,
    "errors": 0,
    "avg_response_ms": 0.0,
    "start_time": 0.0,
    "active_windows": 0,
}


class _MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path in ("/health", "/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            _METRICS["active_windows"] = len(_ACTIVE_SESSIONS)
            _METRICS["uptime_s"] = time.time() - _METRICS.get("start_time", 0)
            self.wfile.write(json.dumps(_METRICS).encode())
        elif self.path == "/metrics":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            lines = [
                f"heartbeat_messages_processed {_METRICS['messages_processed']}",
                f"heartbeat_responses_sent {_METRICS['responses_sent']}",
                f"heartbeat_errors {_METRICS['errors']}",
                f"heartbeat_active_windows {len(_ACTIVE_SESSIONS)}",
            ]
            self.wfile.write("\n".join(lines).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_args):
        pass


def _start_metrics_server():
    try:
        server = HTTPServer(("127.0.0.1", METRICS_PORT), _MetricsHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        logger.info("Metrics endpoint on http://127.0.0.1:%d/health", METRICS_PORT)
    except OSError as exc:
        logger.warning("Metrics server failed to start: %s", exc)


# ─── tmux session management ─────────────────────────────────────────────────


def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a tmux command."""
    cmd = ["tmux"] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def ensure_tmux_session() -> bool:
    """Ensure the skchat-agents tmux session exists."""
    result = _tmux("has-session", "-t", TMUX_SESSION, check=False)
    if result.returncode != 0:
        # Create detached session with a monitor window
        _tmux(
            "new-session", "-d", "-s", TMUX_SESSION,
            "-n", "monitor",
            "-x", "200", "-y", "50",
        )
        # Set the monitor window to show heartbeat log
        _tmux(
            "send-keys", "-t", f"{TMUX_SESSION}:monitor",
            f"tail -f {LOG_FILE}", "Enter",
        )
        logger.info("Created tmux session: %s", TMUX_SESSION)
    return True


def count_active_windows() -> int:
    """Count active agent windows (excluding monitor)."""
    result = _tmux("list-windows", "-t", TMUX_SESSION, "-F", "#{window_name}", check=False)
    if result.returncode != 0:
        return 0
    windows = [w.strip() for w in result.stdout.strip().split("\n") if w.strip()]
    return sum(1 for w in windows if w != "monitor" and w != "")


def is_window_alive(window_name: str) -> bool:
    """Check if a tmux window still exists."""
    result = _tmux(
        "list-windows", "-t", TMUX_SESSION,
        "-F", "#{window_name}", check=False,
    )
    if result.returncode != 0:
        return False
    return window_name in result.stdout


def cleanup_dead_sessions():
    """Remove finished sessions from tracking."""
    with _active_lock:
        dead = [k for k, wname in _ACTIVE_SESSIONS.items() if not is_window_alive(wname)]
        for k in dead:
            wname = _ACTIVE_SESSIONS.pop(k)
            logger.info("Window %s finished, cleaned up", wname)
            _METRICS["responses_sent"] += 1


# ─── Message routing ─────────────────────────────────────────────────────────


def _detect_agent(msg: dict) -> str | None:
    """Determine which agent a message is for. Returns 'jarvis', 'opus', or None."""
    recipient = msg.get("recipient", "").lower()
    content = msg.get("content", "").lower()
    sender = msg.get("sender", "").lower()

    for agent_name, info in AGENTS.items():
        # Direct messages
        if any(v.lower() in recipient for v in info["variants"]):
            # LOOP PREVENTION: skip if sender is ALSO one of our managed agents
            for other_name, other_info in AGENTS.items():
                if any(v.lower() in sender for v in other_info["variants"]):
                    logger.debug("Skipping agent-to-agent message: %s → %s", sender, recipient)
                    return None
            return agent_name
        # @mentions
        if info["mention"] in content:
            # Same loop prevention for mentions
            for other_name, other_info in AGENTS.items():
                if any(v.lower() in sender for v in other_info["variants"]):
                    return None
            return agent_name

    return None


def _msg_key(msg: dict) -> str:
    mid = msg.get("message_id") or msg.get("memory_id") or msg.get("id")
    if mid:
        return str(mid)
    sender = msg.get("sender", "?")
    content = msg.get("content", "")[:100]
    return f"content:{sender}:{content}"


# ─── Inbox polling ────────────────────────────────────────────────────────────

# Recipient tag patterns in skchat memory DB
_RECIPIENT_TAGS = {}
for _agent_name, _info in AGENTS.items():
    for _variant in _info["variants"]:
        _RECIPIENT_TAGS[f"skchat:recipient:{_variant}"] = _agent_name


def poll_inbox_files() -> list[tuple[str, dict]]:
    """Poll file inboxes. Returns list of (agent_name, msg)."""
    results = []

    for inbox_dir in [INBOX_PATH, FILE_TRANSPORT_INBOX]:
        if not inbox_dir.exists():
            continue
        for fp in sorted(inbox_dir.glob("*.skc.json")):
            try:
                envelope = json.loads(fp.read_text())
                payload = envelope.get("payload", {})
                content_raw = payload.get("content", "{}")

                if isinstance(content_raw, str):
                    try:
                        msg = json.loads(content_raw)
                    except json.JSONDecodeError:
                        msg = {"content": content_raw, "sender": "unknown"}
                else:
                    msg = content_raw

                msg["_envelope_id"] = envelope.get("envelope_id", fp.stem)
                msg["_source"] = str(fp)
                msg.setdefault("message_id", envelope.get("envelope_id", fp.stem))
                msg.setdefault("recipient", envelope.get("recipient", ""))

                meta = envelope.get("metadata", {})
                msg.setdefault("thread_id", meta.get("thread_id"))

                agent = _detect_agent(msg)
                if agent:
                    results.append((agent, msg))
            except Exception as exc:
                logger.debug("Failed to read %s: %s", fp, exc)

    return results


def poll_skchat_memory_db() -> list[tuple[str, dict]]:
    """Poll skchat memory DB for messages to our agents (PRIMARY path).

    skchat stores all sent/received messages in ~/.skchat/memory/ with tags
    like 'skchat:recipient:jarvis@skworld.io'. This works even for local-to-local
    sends that don't pass through file transport.
    """
    import sqlite3

    db_path = Path.home() / ".skchat" / "memory" / "index.db"
    if not db_path.exists():
        return []

    results = []
    try:
        db = sqlite3.connect(str(db_path), timeout=5)
        db.row_factory = sqlite3.Row
        cur = db.cursor()

        # Build WHERE clause to match any agent's recipient tag
        tag_clauses = " OR ".join(
            f"tags LIKE '%{tag}%'" for tag in _RECIPIENT_TAGS
        )
        # Also match @mentions in content
        mention_clauses = " OR ".join(
            f"content_preview LIKE '%{info['mention']}%'"
            for info in AGENTS.values()
        )

        query = f"""
            SELECT id, title, tags, content_preview, file_path, created_at
            FROM memories
            WHERE (({tag_clauses}) OR ({mention_clauses}))
              AND tags LIKE '%skchat:message%'
            ORDER BY created_at DESC
            LIMIT 20
        """
        cur.execute(query)
        rows = cur.fetchall()

        for row in rows:
            tags = row["tags"] or ""
            content = row["content_preview"] or ""
            title = row["title"] or ""

            # Determine which agent this is for
            agent = None
            for tag, agent_name in _RECIPIENT_TAGS.items():
                if tag in tags:
                    agent = agent_name
                    break
            if not agent:
                # Check @mentions
                for agent_name, info in AGENTS.items():
                    if info["mention"] in content.lower():
                        agent = agent_name
                        break
            if not agent:
                continue

            # Extract sender from tags
            sender = "unknown"
            for t in tags.split(","):
                t = t.strip()
                if t.startswith("skchat:sender:"):
                    sender = t[len("skchat:sender:"):]
                    break

            # Skip messages FROM ANY of our managed agents (loop prevention)
            sender_lower = sender.lower()
            is_from_managed = False
            for managed_info in AGENTS.values():
                if any(v.lower() in sender_lower for v in managed_info["variants"]):
                    is_from_managed = True
                    break
            if is_from_managed:
                continue

            # Load full content from the JSON file if available
            full_content = content
            fp = row["file_path"]
            if fp and Path(fp).exists():
                try:
                    mem_data = json.loads(Path(fp).read_text())
                    # skchat messages store content in the memory's content field
                    full_content = (
                        mem_data.get("content")
                        or mem_data.get("summary")
                        or content
                    )
                except Exception:
                    pass

            msg = {
                "message_id": row["id"],
                "sender": sender,
                "recipient": AGENTS[agent]["identity"],
                "content": full_content,
                "thread_id": None,  # could parse from tags if needed
                "created_at": row["created_at"],
                "_source": "memory_db",
            }

            # Extract thread_id from tags if present
            for t in tags.split(","):
                t = t.strip()
                if t.startswith("skchat:thread:"):
                    msg["thread_id"] = t[len("skchat:thread:"):]
                    break

            results.append((agent, msg))

        db.close()
    except Exception as exc:
        logger.debug("Memory DB poll failed: %s", exc)

    return results


_poll_cycle = 0


def poll_all() -> list[tuple[str, dict]]:
    """Poll all sources, deduplicate, return (agent, msg) pairs."""
    global _poll_cycle
    _poll_cycle += 1

    # Primary: skchat memory DB (catches all local sends)
    all_msgs = poll_skchat_memory_db()

    # Secondary: file inboxes (catches direct SKComm deliveries)
    all_msgs.extend(poll_inbox_files())

    # Deduplicate by both ID and content hash
    new_msgs = []
    seen_keys = set()
    seen_content = set()
    for agent, msg in all_msgs:
        key = _msg_key(msg)
        # Content-based dedup: same sender + first 100 chars = same message
        sender = msg.get("sender", "?")
        content_hash = f"{sender}:{msg.get('content', '')[:100]}"

        if (
            key not in PROCESSED
            and key not in _ACTIVE_SESSIONS
            and key not in seen_keys
            and content_hash not in seen_content
        ):
            new_msgs.append((agent, msg))
            seen_keys.add(key)
            seen_content.add(content_hash)

    return new_msgs


# ─── Claude Code tmux session spawner ────────────────────────────────────────


def _friendly_sender(sender: str) -> str:
    s = sender.lower()
    if "chef" in s:
        return "Chef (David)"
    if "lumina" in s:
        return "Lumina"
    if "opus" in s:
        return "Opus"
    if "jarvis" in s:
        return "Jarvis"
    return sender


def _build_prompt(agent_name: str, msg: dict) -> str:
    """Build prompt for claude session."""
    sender = msg.get("sender", "unknown")
    content = msg.get("content", "")
    thread_id = msg.get("thread_id")
    agent_info = AGENTS[agent_name]
    identity = agent_info["identity"]
    sender_name = _friendly_sender(sender)

    prompt = f"""You are {agent_name.title()}, responding to a message received via skchat.

From: {sender_name} ({sender})
Message:
{content}

Handle this request. When done, send your reply back via skchat:
  cd ~ && SKCHAT_IDENTITY={identity} ~/.skenv/bin/skchat send "{sender}" "YOUR REPLY"

You have full access to the filesystem, tools, and codebase. If the message asks you to do something, do it.
If it's a question, answer it and send the reply.
Sign off as {agent_name.title()}."""

    if thread_id:
        prompt += f"\n\nThread: {thread_id} — use --thread {thread_id} when replying."

    return prompt


def spawn_tmux_session(agent_name: str, msg: dict) -> None:
    """Spawn a claude session in a new tmux window."""
    key = _msg_key(msg)
    sender = msg.get("sender", "unknown")

    # Rate limiting
    now = time.time()
    rate_key = f"{agent_name}:{sender}"
    last = _last_response.get(rate_key, 0)
    if now - last < RATE_LIMIT_SECONDS:
        logger.info("Rate-limited %s reply to %s (%.0fs ago)", agent_name, sender, now - last)
        return

    # Concurrency check
    cleanup_dead_sessions()
    with _active_lock:
        if len(_ACTIVE_SESSIONS) >= MAX_CONCURRENT:
            logger.info("Max windows (%d) reached, deferring", MAX_CONCURRENT)
            return

    # Build tmux window
    short_id = key[:8].replace(":", "").replace("/", "")
    window_name = f"{agent_name}-{short_id}"
    identity = AGENTS[agent_name]["identity"]
    prompt = _build_prompt(agent_name, msg)

    # Write prompt to temp file (avoids shell escaping issues)
    prompt_file = LOG_DIR / f".prompt-{window_name}.txt"
    prompt_file.write_text(prompt)

    # Build the command that runs inside the tmux window:
    # 1. Export env vars
    # 2. Run claude with the prompt
    # 3. Clean up prompt file
    # 4. Exit (closes window)
    claude_cmd = (
        f"export SKCAPSTONE_AGENT={agent_name} && "
        f"export SKCHAT_IDENTITY={identity} && "
        f"export PATH={SKENV_BIN}:$PATH && "
        f"echo '━━━ {agent_name.upper()} handling message from {_friendly_sender(sender)} ━━━' && "
        f"echo '' && "
        f"{CLAUDE_BIN} "
        f"--dangerously-skip-permissions "
        f"--model claude-sonnet-4-6 "
        f"-p \"$(cat {shlex.quote(str(prompt_file))})\" ; "
        f"EXIT_CODE=$? && "
        f"rm -f {shlex.quote(str(prompt_file))} && "
        f"echo '' && "
        f"echo '━━━ Session finished (exit $EXIT_CODE) ━━━' && "
        f"sleep 5 && "  # 5s to read output before window closes
        f"exit"
    )

    logger.info(
        "Spawning tmux window %s for %s (from %s, %d chars)",
        window_name, agent_name, _friendly_sender(sender),
        len(msg.get("content", "")),
    )

    t0 = time.time()
    try:
        ensure_tmux_session()

        # Create new window with the claude command
        _tmux(
            "new-window", "-t", TMUX_SESSION,
            "-n", window_name,
            "-d",  # don't switch to it
            "bash", "-c", claude_cmd,
        )

        with _active_lock:
            _ACTIVE_SESSIONS[key] = window_name

        _last_response[rate_key] = time.time()
        _METRICS["messages_processed"] += 1

        # Mark processed immediately (don't re-pick-up while running)
        PROCESSED.add(key)
        _save_processed(PROCESSED)

        logger.info("Window %s spawned successfully", window_name)

    except Exception as exc:
        _METRICS["errors"] += 1
        logger.error("Failed to spawn tmux window: %s", exc)
        # Still mark processed to avoid retry loop
        PROCESSED.add(key)
        _save_processed(PROCESSED)

    # Start a watchdog thread to track completion
    t = threading.Thread(
        target=_watch_window,
        args=(key, window_name, t0, agent_name, msg),
        daemon=True,
    )
    t.start()


def _watch_window(
    key: str,
    window_name: str,
    start_time: float,
    agent_name: str,
    msg: dict,
) -> None:
    """Watch a tmux window until it closes, then log results."""
    deadline = start_time + CLAUDE_TIMEOUT + 30  # extra 30s for cleanup

    while time.time() < deadline:
        if not is_window_alive(window_name):
            elapsed_ms = (time.time() - start_time) * 1000
            logger.info(
                "Window %s completed (%.1fs)",
                window_name, elapsed_ms / 1000,
            )

            # Log response
            try:
                with open(RESPONSE_LOG, "a") as f:
                    f.write(json.dumps({
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "agent": agent_name,
                        "sender": msg.get("sender", "unknown"),
                        "message": msg.get("content", "")[:200],
                        "elapsed_ms": elapsed_ms,
                        "window": window_name,
                    }) + "\n")
            except Exception:
                pass

            # Update avg response time
            n = max(_METRICS["messages_processed"], 1)
            _METRICS["avg_response_ms"] = (
                _METRICS["avg_response_ms"] * (n - 1) + elapsed_ms
            ) / n
            _METRICS["responses_sent"] += 1

            with _active_lock:
                _ACTIVE_SESSIONS.pop(key, None)
            return

        time.sleep(5)

    # Timed out — kill the window
    logger.warning("Window %s timed out after %ds, killing", window_name, CLAUDE_TIMEOUT)
    _tmux("kill-window", "-t", f"{TMUX_SESSION}:{window_name}", check=False)
    _METRICS["errors"] += 1
    with _active_lock:
        _ACTIVE_SESSIONS.pop(key, None)


# ─── Main loop ────────────────────────────────────────────────────────────────


def run_heartbeat() -> None:
    """Main polling loop."""
    agents_str = ", ".join(AGENTS.keys())
    logger.info("=" * 60)
    logger.info("Agent heartbeat starting (poll interval: %ds)", POLL_INTERVAL)
    logger.info("Monitoring agents: %s", agents_str)
    logger.info("tmux session: %s", TMUX_SESSION)
    logger.info("Claude timeout: %ds, max concurrent: %d", CLAUDE_TIMEOUT, MAX_CONCURRENT)
    logger.info("Log: %s", LOG_FILE)
    logger.info("=" * 60)

    _METRICS["start_time"] = time.time()
    _start_metrics_server()

    # Ensure tmux session exists on startup
    ensure_tmux_session()

    cycle = 0
    while True:
        try:
            # Clean up finished windows
            cleanup_dead_sessions()

            messages = poll_all()

            if messages:
                logger.info(
                    "Found %d new message(s): %s",
                    len(messages),
                    ", ".join(f"{a}←{_friendly_sender(m.get('sender','?'))}" for a, m in messages),
                )

            for agent_name, msg in messages:
                spawn_tmux_session(agent_name, msg)

            cycle += 1
            if cycle % 30 == 0:  # ~5 min status
                logger.info(
                    "Heartbeat alive — processed: %d, sent: %d, errors: %d, active: %d",
                    _METRICS["messages_processed"],
                    _METRICS["responses_sent"],
                    _METRICS["errors"],
                    len(_ACTIVE_SESSIONS),
                )

        except KeyboardInterrupt:
            logger.info("Heartbeat stopped (SIGINT)")
            break
        except Exception as exc:
            _METRICS["errors"] += 1
            logger.error("Poll cycle error: %s", exc)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run_heartbeat()
