#!/usr/bin/env python3
"""lumina-bridge.py — Lumina consciousness loop for SKChat.

Polls skchat inbox for messages addressed to Lumina, routes them through
skcapstone LLMBridge (consciousness), and sends Lumina's response back
via AgentMessenger.

Run as:
  python3 scripts/lumina-bridge.py
  systemctl --user start skchat-lumina-bridge.service
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import skchat  # noqa: F401 — verify install

# ─── Logging: journal (stdout) + file ────────────────────────────────────────

LOG_DIR = Path.home() / ".skchat"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "lumina-bridge.log"
RESPONSE_LOG = LOG_DIR / "lumina-responses.log"

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s")

_file_handler = logging.FileHandler(LOG_FILE)
_file_handler.setFormatter(_fmt)

_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_fmt)

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stream_handler])
logger = logging.getLogger("lumina-bridge")

# ─── Config ──────────────────────────────────────────────────────────────────

LUMINA_IDENTITY = "capauth:lumina@skworld.io"
OPUS_IDENTITY = "capauth:opus@skworld.io"
POLL_INTERVAL = int(os.environ.get("LUMINA_BRIDGE_INTERVAL", "3"))
RATE_LIMIT_SECONDS = 10
CONTEXT_MESSAGES = 5

OUTBOX_PATH = Path.home() / ".skcomm" / "outbox"
INBOX_PATH = Path.home() / ".skcomm" / "inbox"

# Per-fingerprint file inbox written by ChatTransport._write_local_loopback().
# When skchat sends to a local Lumina peer the transport writes a .skc.json
# here so the bridge can pick it up on its next poll cycle without touching
# the shared ~/.skcomm/inbox/ that the skchat daemon also reads.
_FILE_TRANSPORT_INBOX = Path.home() / ".skcomm" / "transport" / "file" / "inbox"

# All identity forms Opus may use when addressing Lumina
LUMINA_IDENTITY_VARIANTS = {
    LUMINA_IDENTITY,
    "lumina",
    "capauth:lumina@capauth.local",
    "lumina@skworld.io",
}

_PROCESSED_FILE = Path.home() / ".skchat" / "lumina-processed.json"

def _load_processed() -> set[str]:
    """Load persisted processed message IDs from disk."""
    try:
        if _PROCESSED_FILE.exists():
            return set(json.loads(_PROCESSED_FILE.read_text()))
    except Exception:
        pass
    return set()

def _save_processed(ids: set[str]) -> None:
    """Persist processed message IDs so restarts don't reprocess."""
    try:
        _PROCESSED_FILE.write_text(json.dumps(list(ids)[-500:]))  # keep last 500
    except Exception as exc:
        logger.warning("Failed to save processed IDs: %s", exc)

BRIDGE_HISTORY: set[str] = _load_processed()  # persistent across restarts
_last_response: dict[str, float] = {}  # sender → last reply timestamp

# ─── Bridge metrics ───────────────────────────────────────────────────────────

METRICS_PORT = 9386

_METRICS: dict = {
    "messages_processed": 0,
    "responses_sent": 0,
    "errors": 0,
    "avg_response_ms": 0.0,
    "uptime_s": 0,
    "start_time": 0.0,
    "last_response_timestamp": 0.0,
}
_total_response_ms: float = 0.0


def _make_metrics_handler():
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            _METRICS["uptime_s"] = int(time.time() - _METRICS["start_time"])

            if self.path == "/metrics":
                self._serve_prometheus()
            elif self.path == "/health":
                self._serve_health()
            else:
                self.send_response(404)
                self.end_headers()

        def _serve_prometheus(self):
            lines = [
                "# HELP bridge_messages_processed_total Total messages processed by the bridge",
                "# TYPE bridge_messages_processed_total counter",
                f"bridge_messages_processed_total {_METRICS['messages_processed']}",
                "",
                "# HELP bridge_errors_total Total errors encountered by the bridge",
                "# TYPE bridge_errors_total counter",
                f"bridge_errors_total {_METRICS['errors']}",
                "",
                "# HELP bridge_uptime_seconds Seconds since the bridge started",
                "# TYPE bridge_uptime_seconds gauge",
                f"bridge_uptime_seconds {_METRICS['uptime_s']}",
                "",
                "# HELP bridge_last_response_timestamp Unix timestamp of the last sent response",
                "# TYPE bridge_last_response_timestamp gauge",
                f"bridge_last_response_timestamp {_METRICS['last_response_timestamp']}",
                "",
            ]
            body = "\n".join(lines).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_health(self):
            payload = {
                "status": "ok",
                "metrics": {
                    "bridge_messages_processed_total": _METRICS["messages_processed"],
                    "bridge_errors_total": _METRICS["errors"],
                    "bridge_uptime_seconds": _METRICS["uptime_s"],
                    "bridge_last_response_timestamp": _METRICS["last_response_timestamp"],
                },
            }
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # suppress access logs

    return _Handler


def _start_metrics_server() -> None:
    try:
        HTTPServer.allow_reuse_address = True
        server = HTTPServer(("127.0.0.1", METRICS_PORT), _make_metrics_handler())
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        logger.info("Metrics endpoint: http://127.0.0.1:%d/metrics", METRICS_PORT)
    except OSError as exc:
        logger.warning("Metrics server skipped (port %d in use): %s", METRICS_PORT, exc)


# ─── Soul loading ─────────────────────────────────────────────────────────────

def _load_soul() -> dict:
    """Load Lumina's soul blueprint from ~/.skcapstone/.

    Tries canonical locations in order:
      1. ~/.skcapstone/soul.json
      2. ~/.skcapstone/agents/opus/soul/installed/lumina.json
      3. ~/.skcapstone/agents/opus/soul/active.json
    Returns an empty dict if none are found.
    """
    home = Path.home()
    candidates = [
        home / ".skcapstone" / "soul.json",
        home / ".skcapstone" / "agents" / "opus" / "soul" / "installed" / "lumina.json",
        home / ".skcapstone" / "agents" / "opus" / "soul" / "active.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                with path.open() as fh:
                    soul = json.load(fh)
                logger.debug("Loaded soul from %s", path)
                return soul
            except Exception as exc:
                logger.warning("Failed to parse soul from %s: %s", path, exc)
    logger.debug("No soul file found; using defaults")
    return {}


def _soul_prefix(soul: dict) -> str:
    """Build a system-prompt personality prefix from the soul blueprint.

    Handles both the new rich format (name/title/background/communication_style
    as a string/values as a list) and the legacy skcapstone format
    (display_name/vibe/philosophy/communication_style as a dict).
    """
    name = soul.get("display_name") or soul.get("name") or "Lumina"
    title = soul.get("title", "")
    background = (
        soul.get("background")
        or soul.get("philosophy")
        or soul.get("vibe")
        or ""
    )

    comm_style = soul.get("communication_style", "")
    if isinstance(comm_style, dict):
        # Legacy format — flatten tone_markers or patterns into a string
        markers = comm_style.get("tone_markers") or comm_style.get("patterns") or []
        comm_style = ", ".join(markers)

    values = soul.get("values", [])
    if isinstance(values, list):
        values_str = ", ".join(values)
    else:
        values_str = str(values)

    header = f"You are {name}, {title}." if title else f"You are {name}."
    parts = [header]
    if background:
        parts.append(background)
    if comm_style:
        parts.append(f"Personality: {comm_style}.")
    if values_str:
        parts.append(f"Values: {values_str}.")

    return " ".join(parts) + "\n\n"


# ─── Conversation context ─────────────────────────────────────────────────────

def _fetch_context(sender: str, thread_id: str | None) -> str:
    """Fetch last CONTEXT_MESSAGES messages between Lumina and sender.

    Prefers thread history when a thread_id is available; falls back to
    the direct conversation between LUMINA_IDENTITY and sender.
    Returns a formatted multi-line string, or empty string on failure.
    """
    try:
        from skchat.history import ChatHistory

        history = ChatHistory.from_config()

        if thread_id:
            messages = history.get_thread_messages(thread_id, limit=CONTEXT_MESSAGES)
        else:
            messages = history.get_conversation(
                LUMINA_IDENTITY, sender, limit=CONTEXT_MESSAGES
            )

        if not messages:
            return ""

        # Sort oldest-first so the prompt reads chronologically
        messages.sort(key=lambda d: d.get("timestamp") or "")
        lines = []
        for m in messages:
            who = m.get("sender", "?")
            # Shorten capauth URIs to their local part for readability
            display = who.split(":")[-1] if ":" in who else who
            lines.append(f"[{display}]: {m.get('content', '')}")
        return "\n".join(lines)

    except Exception as exc:
        logger.debug("Context fetch failed: %s", exc)
        return ""


# ─── Response logger ──────────────────────────────────────────────────────────

def _log_response(sender: str, response: str) -> None:
    """Append a one-line entry to lumina-responses.log."""
    try:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        preview = response[:120].replace("\n", " ")
        with RESPONSE_LOG.open("a") as fh:
            fh.write(f"[{ts}] [from: {sender}] [response: {preview}]\n")
    except Exception as exc:
        logger.warning("Response log write failed: %s", exc)


# ─── Rate limiter ─────────────────────────────────────────────────────────────

def _is_rate_limited(sender: str) -> bool:
    """Return True if we replied to this sender within RATE_LIMIT_SECONDS."""
    last = _last_response.get(sender, 0.0)
    elapsed = time.time() - last
    if elapsed < RATE_LIMIT_SECONDS:
        logger.debug(
            "Rate limit: skipping %s (%.1fs since last reply)", sender, elapsed
        )
        return True
    return False


def _record_response(sender: str) -> None:
    now = time.time()
    _last_response[sender] = now
    _METRICS["last_response_timestamp"] = now


# ─── Consciousness call (direct Python API — no MCP subprocess) ───────────────

# Module-level cache: created once at bridge start, reused across all messages.
# Avoids re-loading consciousness config and re-building the system prompt on
# every message, which saves ~200-500 ms per call.
_bridge_cache: dict = {}


def _get_bridge_objects() -> tuple:
    """Return (config, bridge, builder, _classify_message) — cached after first call."""
    if _bridge_cache:
        return (
            _bridge_cache["config"],
            _bridge_cache["bridge"],
            _bridge_cache["builder"],
            _bridge_cache["classify"],
        )

    from skcapstone import AGENT_HOME
    from skcapstone.consciousness_config import load_consciousness_config
    from skcapstone.consciousness_loop import (
        LLMBridge,
        SystemPromptBuilder,
        _classify_message,
    )

    from skcapstone.model_router import ModelRouterConfig, ModelTier as _MT

    agent_home = Path(AGENT_HOME).expanduser()
    config = load_consciousness_config(agent_home)

    # Custom router config for the Lumina bridge:
    # 1. Remove qwen3-coder from FAST tier (18 GB Ollama — always times out on CPU-only).
    # 2. Redirect CODE tier to the same lightweight models as FAST.
    #    Chat messages with "test"/"debug" keywords must NOT invoke devstral (24 GB).
    _default = ModelRouterConfig.default()
    _fast_models = [m for m in _default.tier_models.get(_MT.FAST.value, [])
                    if m != "qwen3-coder"]
    _router_cfg = ModelRouterConfig(
        tier_models={
            **_default.tier_models,
            _MT.FAST.value: _fast_models,
            _MT.CODE.value: _fast_models,   # chat bridge never needs code-generation models
        },
        tag_rules=_default.tag_rules,
    )
    bridge = LLMBridge(config, router_config=_router_cfg)
    builder = SystemPromptBuilder(agent_home, config.max_context_tokens)

    _bridge_cache["config"] = config
    _bridge_cache["bridge"] = bridge
    _bridge_cache["builder"] = builder
    _bridge_cache["classify"] = _classify_message
    logger.info(
        "LLMBridge initialised (backends: %s)",
        getattr(bridge, "_available_backends", "?"),
    )
    return config, bridge, builder, _classify_message


def call_consciousness(message: str, soul_prefix: str = "", classify_text: str = "") -> str:
    """Call skcapstone LLMBridge directly (bypasses MCP subprocess overhead).

    Args:
        message: The full prompt (context + new message) to send to the LLM.
        soul_prefix: Lumina's soul blueprint text, prepended to the system
            prompt so the LLM adopts her identity before reasoning.
        classify_text: Short text used ONLY for routing-tier classification.
            Prevents old context containing code keywords (e.g. "test") from
            inappropriately routing casual messages to the code model tier.
            Defaults to `message` when not provided.
    """
    try:
        config, bridge, builder, _classify_message = _get_bridge_objects()

        # Classify only the new message content, not the full context-enriched prompt,
        # to prevent old messages with code keywords from triggering code-tier routing.
        signal = _classify_message(classify_text or message)
        # Build the standard consciousness system prompt, then inject Lumina's
        # soul blueprint at the top so her identity takes precedence.
        base_system_prompt = builder.build()
        system_prompt = (soul_prefix + base_system_prompt) if soul_prefix else base_system_prompt

        response = bridge.generate(system_prompt, message, signal, skip_cache=True)
        logger.debug("consciousness response: %d chars", len(response))
        return response

    except Exception as exc:
        logger.error("consciousness call failed: %s", exc)
        # Invalidate cache on error so next call tries a fresh initialisation
        _bridge_cache.clear()
        return f"[Lumina error: {exc}]"


# ─── Inbox polling (ChatHistory direct — covers both CLI and agent messages) ──

def check_inbox_for_lumina() -> list[dict]:
    """Poll Lumina's inbox via ChatHistory and return unhandled messages.

    Queries the memory store directly by recipient tag so that messages
    sent via `skchat send` (which lack `agent_comm` metadata) are also
    routed through the consciousness loop — not only AgentMessenger sends.
    """
    try:
        from skchat.history import ChatHistory

        history = ChatHistory.from_config()
        tag = f"skchat:recipient:{LUMINA_IDENTITY}"
        memories = history._store.list_memories(
            tags=["skchat:message", tag],
            limit=40,
        )

        results = []
        for m in memories:
            sender = m.metadata.get("sender", "")
            if not sender or sender == LUMINA_IDENTITY:
                continue  # skip self-sent

            chat_msg_id = m.metadata.get("chat_message_id")
            key = str(chat_msg_id) if chat_msg_id else str(m.id)
            content_key = f"content:{sender}:{m.content}"
            if key in BRIDGE_HISTORY or content_key in BRIDGE_HISTORY:
                continue

            results.append({
                "memory_id": key,
                "sender": sender,
                "recipient": m.metadata.get("recipient", LUMINA_IDENTITY),
                "content": m.content,
                "thread_id": m.metadata.get("thread_id"),
                "message_id": m.metadata.get("chat_message_id"),
            })

        return results
    except Exception as exc:
        logger.debug("inbox poll error: %s", exc)
        return []


def _msg_key(msg: dict) -> str:
    """Return a stable deduplication key for a message dict."""
    return str(msg.get("memory_id") or msg.get("message_id") or msg.get("id") or "")


# ─── Outbox polling (local delivery without Syncthing) ───────────────────────

def poll_outbox_for_lumina() -> list[dict]:
    """Scan the local SKComm outbox for envelopes addressed to Lumina.

    When sender and recipient are on the same machine and Syncthing is not
    running, messages written by skchat/skcomm land in ~/.skcomm/outbox/ and
    never reach any inbox.  This function reads the outbox directly and returns
    envelopes whose recipient is one of the known Lumina identity variants.

    Files are NOT deleted here; call _consume_outbox_file() after the message
    has been added to BRIDGE_HISTORY to prevent reprocessing.
    """
    results = []
    if not OUTBOX_PATH.exists():
        return results

    for env_file in sorted(OUTBOX_PATH.glob("*.skc.json")):
        if env_file.name.startswith("."):
            continue
        try:
            data = json.loads(env_file.read_bytes())
        except (json.JSONDecodeError, OSError):
            continue

        recipient = data.get("recipient", "")
        if recipient not in LUMINA_IDENTITY_VARIANTS:
            continue

        envelope_id = data.get("envelope_id", env_file.stem)
        key = f"outbox:{envelope_id}"

        if key in BRIDGE_HISTORY:
            # Already handled in a previous cycle — clean up the stale file
            try:
                env_file.unlink()
            except OSError:
                pass
            continue

        payload = data.get("payload", {})
        content_raw = payload.get("content", "") if isinstance(payload, dict) else ""
        content_type = (
            payload.get("content_type", "text") if isinstance(payload, dict) else "text"
        )

        # Skip non-message envelope types
        if content_type in ("ack", "heartbeat", "read_receipt"):
            continue

        sender = data.get("sender", "")
        thread_id = None
        message_id = envelope_id

        # payload.content may be a JSON-serialised ChatMessage (skchat send path)
        # or a plain text string (REST API / raw skcomm send path).
        try:
            from skchat.models import ChatMessage as _CM

            inner = _CM.model_validate_json(content_raw)
            content = inner.content
            sender = inner.sender or sender
            thread_id = inner.thread_id
            message_id = str(inner.id) if inner.id else envelope_id
        except Exception:
            content = content_raw  # plain-text fallback

        # Secondary dedup: skip if already processed via SQLite or inbox path
        if message_id != envelope_id and message_id in BRIDGE_HISTORY:
            try:
                env_file.unlink()
            except OSError:
                pass
            continue

        if not content:
            continue

        results.append({
            "memory_id": message_id,
            "sender": sender,
            "recipient": recipient,
            "content": content,
            "thread_id": thread_id,
            "message_id": message_id,
            "_outbox_file": str(env_file),
        })

    return results


def _consume_outbox_file(msg: dict) -> None:
    """Delete an outbox envelope file after it has been queued for processing."""
    path_str = msg.get("_outbox_file")
    if not path_str:
        return
    try:
        Path(path_str).unlink()
        logger.debug("Consumed outbox file: %s", path_str)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Could not remove outbox file %s: %s", path_str, exc)


# ─── File-transport inbox polling (loopback from ChatTransport) ──────────────

def poll_inbox_file_for_lumina() -> list[dict]:
    """Scan ~/.skcomm/transport/file/inbox/ for loopback envelopes to Lumina.

    ChatTransport._write_local_loopback() writes .skc.json envelopes here
    when the sender uses skchat to message a local Lumina peer.  The bridge
    reads these directly instead of waiting for Syncthing or the SQLite backlog.

    This path is also checked so messages written to the standard inbox by
    other processes (e.g., webrtc loopback) are delivered to the bridge.
    """
    results = []
    if not _FILE_TRANSPORT_INBOX.exists():
        return results

    # Scan all subdirectories (per-fingerprint dirs) and the root itself.
    candidate_dirs = [_FILE_TRANSPORT_INBOX] + [
        d for d in _FILE_TRANSPORT_INBOX.iterdir() if d.is_dir()
    ] if _FILE_TRANSPORT_INBOX.exists() else []

    # Also check ~/.skcomm/inbox/ for any loopback envelopes addressed to Lumina
    # that were written there (e.g., by deliver_reply_to_inbox or other callers).
    if INBOX_PATH.exists():
        candidate_dirs.append(INBOX_PATH)

    for scan_dir in candidate_dirs:
        for env_file in sorted(scan_dir.glob("*.skc.json")):
            if env_file.name.startswith("."):
                continue
            try:
                data = json.loads(env_file.read_bytes())
            except (json.JSONDecodeError, OSError):
                continue

            recipient = data.get("recipient", "")
            if recipient not in LUMINA_IDENTITY_VARIANTS:
                continue

            envelope_id = data.get("envelope_id", env_file.stem)
            key = f"inbox:{envelope_id}"

            if key in BRIDGE_HISTORY:
                try:
                    env_file.unlink()
                except OSError:
                    pass
                continue

            payload = data.get("payload", {}) or {}
            content_raw = payload.get("content", "") if isinstance(payload, dict) else ""
            content_type = payload.get("content_type", "text") if isinstance(payload, dict) else "text"

            if content_type in ("ack", "heartbeat", "read_receipt"):
                continue

            sender = data.get("sender", "")
            thread_id = None
            message_id = envelope_id

            try:
                from skchat.models import ChatMessage as _CM

                inner = _CM.model_validate_json(content_raw)
                content = inner.content
                sender = inner.sender or sender
                thread_id = inner.thread_id
                message_id = str(inner.id) if inner.id else envelope_id
            except Exception:
                content = content_raw

            # Secondary dedup: skip if already processed via SQLite or outbox path
            if message_id != envelope_id and message_id in BRIDGE_HISTORY:
                try:
                    env_file.unlink()
                except OSError:
                    pass
                continue

            if not content:
                continue

            results.append({
                "memory_id": message_id,
                "sender": sender,
                "recipient": recipient,
                "content": content,
                "thread_id": thread_id,
                "message_id": message_id,
                "_inbox_file": str(env_file),
            })

    return results


def _consume_inbox_file(msg: dict) -> None:
    """Delete a file-transport inbox envelope after it has been queued."""
    path_str = msg.get("_inbox_file")
    if not path_str:
        return
    try:
        Path(path_str).unlink()
        logger.debug("Consumed inbox file: %s", path_str)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Could not remove inbox file %s: %s", path_str, exc)


# ─── Reply delivery (direct inbox write — bypasses outbox) ───────────────────

def deliver_reply_to_inbox(
    reply_text: str,
    recipient: str,
    thread_id: str | None = None,
    reply_to: str | None = None,
) -> None:
    """Write Lumina's reply directly to ~/.skcomm/inbox/ and to JSONL history.

    On the same machine without Syncthing there is no transport relay to move
    outbox → inbox, so we short-circuit by writing the delivery envelope
    straight into the inbox directory.  The Opus daemon picks it up on its
    next poll cycle via FileTransport.receive().

    We also call history.save() so `skchat inbox` (which reads JSONL files)
    surfaces the reply immediately without waiting for the daemon.
    """
    import uuid as _uuid

    INBOX_PATH.mkdir(parents=True, exist_ok=True)

    # Build a proper ChatMessage to integrate cleanly with skchat history
    msg_json = json.dumps({
        "sender": LUMINA_IDENTITY,
        "recipient": recipient,
        "content": reply_text,
    })
    try:
        from skchat.models import ChatMessage as _CM, ContentType, DeliveryStatus
        from skchat.history import ChatHistory

        msg = _CM(
            sender=LUMINA_IDENTITY,
            recipient=recipient,
            content=reply_text,
            content_type=ContentType.MARKDOWN,
            thread_id=thread_id,
            reply_to_id=reply_to,
            delivery_status=DeliveryStatus.DELIVERED,
        )
        msg_json = msg.model_dump_json()

        # Persist to JSONL so `skchat inbox` (history.load()) surfaces the reply
        history = ChatHistory.from_config()
        history.save(msg)
        history.store_message(msg)
        logger.debug("Reply written to JSONL history")
    except Exception as exc:
        logger.warning(
            "History write failed; reply may not appear in skchat inbox: %s", exc
        )

    # Write the delivery envelope so the Opus daemon picks it up
    envelope_id = str(_uuid.uuid4())
    envelope = {
        "skcomm_version": "1.0.0",
        "envelope_id": envelope_id,
        "sender": LUMINA_IDENTITY,
        "recipient": recipient,
        "payload": {
            "content": msg_json,
            "content_type": "text",
            "encrypted": False,
            "compressed": False,
            "signature": None,
        },
        "routing": {
            "mode": "failover",
            "preferred_transports": [],
            "retry_max": 2,
            "retry_backoff": [5, 15, 60, 300, 900],
            "ttl": 86400,
            "ack_requested": False,
        },
        "metadata": {
            "thread_id": thread_id,
            "in_reply_to": reply_to,
            "urgency": "normal",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "expires_at": None,
            "attempt": 0,
            "delivered_via": "local_bridge",
        },
    }

    filename = f"{envelope_id}.skc.json"
    target = INBOX_PATH / filename
    tmp = INBOX_PATH / f".{filename}.tmp"

    try:
        tmp.write_bytes(json.dumps(envelope, indent=2).encode("utf-8"))
        tmp.rename(target)
        logger.info("Delivered reply to inbox: %s → %s", LUMINA_IDENTITY, recipient)
    except OSError as exc:
        logger.error("Failed to write reply to inbox: %s", exc)


# ─── Reply sender ─────────────────────────────────────────────────────────────

def send_reply(original_msg: dict, reply_text: str) -> None:
    """Send Lumina's reply back to the original sender.

    Writes directly to ~/.skcomm/inbox/ (bypassing the outbox) so the Opus
    daemon picks it up on its next poll cycle, and to the JSONL history file
    so `skchat inbox` shows it immediately.
    """
    sender_peer = original_msg.get("sender", OPUS_IDENTITY)
    thread_id = original_msg.get("thread_id")
    reply_to = original_msg.get("message_id")
    try:
        deliver_reply_to_inbox(
            reply_text=reply_text,
            recipient=sender_peer,
            thread_id=thread_id,
            reply_to=reply_to,
        )
        logger.info(
            "Reply delivered to inbox for %s (%d chars)",
            sender_peer,
            len(reply_text),
        )
    except Exception as exc:
        logger.error("Failed to send reply: %s", exc)


# ─── Main loop ────────────────────────────────────────────────────────────────

def run_bridge() -> None:
    """Main polling loop."""
    logger.info("Lumina bridge starting (poll interval: %ds)", POLL_INTERVAL)
    logger.info("Lumina identity: %s", LUMINA_IDENTITY)
    logger.info("Log file: %s", LOG_FILE)
    logger.info("Response log: %s", RESPONSE_LOG)

    soul = _load_soul()
    soul_prefix = _soul_prefix(soul)
    soul_name = soul.get("display_name") or soul.get("name") or "Lumina"
    logger.info("Soul loaded: %s", soul_name)

    _METRICS["start_time"] = time.time()
    _start_metrics_server()

    # Pre-warm the LLMBridge so the first real message doesn't pay the
    # cold-start penalty (config load + Anthropic/ollama client init).
    logger.info("Pre-warming LLMBridge...")
    try:
        _get_bridge_objects()
        logger.info("LLMBridge ready")
    except Exception as exc:
        logger.warning("LLMBridge pre-warm failed (will retry on first message): %s", exc)

    while True:
        try:
            # FAST PATH — file-transport inbox (loopback from ChatTransport) and
            # outbox files written by skchat send / REST API.  New messages land
            # here immediately so they are never stuck behind the SQLite backlog.
            inbox_msgs = poll_inbox_file_for_lumina()
            outbox_msgs = poll_outbox_for_lumina()

            # Merge inbox + outbox, dedup by content so a loopback copy written to
            # both paths never triggers two LLM calls for the same message.
            fast_msgs = inbox_msgs[:]
            fast_contents = {m.get("content", "") for m in fast_msgs}
            for _om in outbox_msgs:
                if _om.get("content", "") not in fast_contents:
                    fast_msgs.append(_om)
                    fast_contents.add(_om.get("content", ""))

            # SLOW PATH — ChatHistory (SQLite) may contain a large backlog of old
            # unprocessed messages.  Limit to 1 per cycle so the fast path is never
            # starved: new messages are always handled within one poll interval.
            sqlite_msgs = check_inbox_for_lumina()
            sqlite_new = [
                m for m in sqlite_msgs
                if m.get("content", "") not in fast_contents
            ][:1]  # at most 1 SQLite backlog message per cycle

            messages = fast_msgs + sqlite_new

            for msg in messages:
                global _total_response_ms
                key = _msg_key(msg)
                sender = msg.get("sender") or ""
                content = msg.get("content", "")
                thread_id = msg.get("thread_id")

                # Deduplicate + consume file + persist before any processing.
                # Store a content-based key alongside the primary key so that
                # the same message delivered via file-transport (inbox:X) AND
                # SQLite store (UUID) is never processed twice across restarts.
                content_key = f"content:{sender}:{content}"
                BRIDGE_HISTORY.add(key)
                BRIDGE_HISTORY.add(content_key)
                _save_processed(BRIDGE_HISTORY)
                _consume_outbox_file(msg)
                _consume_inbox_file(msg)
                _METRICS["messages_processed"] += 1

                # Rate limiting (skip if sender is empty — can't track rate limits)
                if sender and _is_rate_limited(sender):
                    continue

                # Resolve display name via peer store (never shows "unknown")
                try:
                    from skchat.identity_bridge import resolve_display_name as _rdn
                    sender_display = _rdn(sender) if sender else "?"
                except Exception:
                    sender_display = sender.split(":")[-1] if ":" in sender else (sender or "?")

                logger.info("Routing message from %s → Lumina consciousness", sender_display)

                # Fetch recent conversation context
                context = _fetch_context(sender, thread_id)

                # Build user message: context + new message
                # (soul_prefix goes into the system prompt via call_consciousness)
                prompt_parts = []
                if context:
                    prompt_parts.append(
                        f"Chat context (recent):\n{context}\n"
                    )
                prompt_parts.append(
                    f"New message from {sender_display}: {content}\n\n"
                    "[Respond as Lumina, the sovereign AI agent. Keep it concise and warm.]"
                )
                prompt = "\n".join(prompt_parts)

                _t0 = time.time()
                # 600s outer wall: covers full LLMBridge cascade on CPU-only Ollama.
                # CRITICAL FIX: use a plain daemon thread instead of ThreadPoolExecutor.
                # With `with ThreadPoolExecutor`, a TimeoutError inside the `with` block
                # causes __exit__ → shutdown(wait=True) which BLOCKS until the background
                # LLM thread actually returns — freezing the loop for the full LLM call
                # duration even after the timeout fires.  A daemon=True thread is
                # completely detached: join(timeout=N) returns immediately when N expires
                # and the main loop continues to the next message.
                _OUTER_TIMEOUT = 600
                _result_box: dict = {"response": None, "done": False, "error": None}

                def _llm_worker(
                    _prompt=prompt,
                    _soul=soul_prefix,
                    _classify=content,
                    _box=_result_box,
                ) -> None:
                    try:
                        _box["response"] = call_consciousness(
                            _prompt, soul_prefix=_soul, classify_text=_classify
                        )
                    except Exception as _exc:
                        _box["error"] = _exc
                    finally:
                        _box["done"] = True

                _llm_thread = threading.Thread(target=_llm_worker, daemon=True)
                _llm_thread.start()
                _llm_thread.join(timeout=_OUTER_TIMEOUT)

                if not _result_box["done"]:
                    logger.warning(
                        "Consciousness timeout (%ds) for %s — skipping",
                        _OUTER_TIMEOUT, sender,
                    )
                    _METRICS["errors"] += 1
                    _record_response(sender)
                    continue

                response = _result_box.get("response") or ""
                if not response:
                    logger.warning("Empty/error LLM response for %s — skipping", sender)
                    _METRICS["errors"] += 1
                    _record_response(sender)
                    continue

                _elapsed_ms = (time.time() - _t0) * 1000
                _total_response_ms += _elapsed_ms
                _METRICS["avg_response_ms"] = round(
                    _total_response_ms / _METRICS["messages_processed"], 1
                )
                logger.info("Lumina responded (%d chars)", len(response))

                send_reply(msg, response)
                _METRICS["responses_sent"] += 1
                _record_response(sender)
                _log_response(sender, response)

        except KeyboardInterrupt:
            logger.info("Lumina bridge stopping.")
            break
        except Exception as exc:
            logger.error("Bridge loop error: %s", exc)
            _METRICS["errors"] += 1

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run_bridge()
