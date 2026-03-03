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

BRIDGE_HISTORY: set[str] = set()  # memory_ids already handled
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
}
_total_response_ms: float = 0.0


def _make_metrics_handler():
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/metrics":
                self.send_response(404)
                self.end_headers()
                return
            _METRICS["uptime_s"] = int(time.time() - _METRICS["start_time"])
            body = json.dumps(_METRICS).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # suppress access logs

    return _Handler


def _start_metrics_server() -> None:
    server = HTTPServer(("127.0.0.1", METRICS_PORT), _make_metrics_handler())
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    logger.info("Metrics endpoint: http://127.0.0.1:%d/metrics", METRICS_PORT)


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
    _last_response[sender] = time.time()


# ─── Consciousness call (direct Python API — no MCP subprocess) ───────────────

def call_consciousness(message: str, soul_prefix: str = "") -> str:
    """Call skcapstone LLMBridge directly (bypasses MCP subprocess overhead).

    Args:
        message: The user message to process through the consciousness loop.
        soul_prefix: Lumina's soul blueprint text, prepended to the system
            prompt so the LLM adopts her identity before reasoning.
    """
    try:
        from skcapstone import AGENT_HOME
        from skcapstone.consciousness_config import load_consciousness_config
        from skcapstone.consciousness_loop import (
            LLMBridge,
            SystemPromptBuilder,
            _classify_message,
        )

        # Use the skcapstone agent home (~/.skcapstone), not the bare user home
        agent_home = Path(AGENT_HOME).expanduser()
        config = load_consciousness_config(agent_home)
        bridge = LLMBridge(config)
        builder = SystemPromptBuilder(agent_home, config.max_context_tokens)

        signal = _classify_message(message)
        # Build the standard consciousness system prompt, then inject Lumina's
        # soul blueprint at the top so her identity takes precedence.
        base_system_prompt = builder.build()
        system_prompt = (soul_prefix + base_system_prompt) if soul_prefix else base_system_prompt

        response = bridge.generate(system_prompt, message, signal, skip_cache=True)
        logger.debug("consciousness response: %d chars", len(response))
        return response

    except Exception as exc:
        logger.error("consciousness call failed: %s", exc)
        return f"[Lumina error: {exc}]"


# ─── Inbox polling (AgentMessenger Python API — no CLI subprocess) ────────────

def check_inbox_for_lumina() -> list[dict]:
    """Poll Lumina's inbox via AgentMessenger and return unhandled messages."""
    try:
        from skchat.agent_comm import AgentMessenger

        messenger = AgentMessenger.from_identity(identity=LUMINA_IDENTITY)
        messages = messenger.receive(limit=20)
        return [
            m for m in messages
            if _msg_key(m) not in BRIDGE_HISTORY
            and m.get("sender") != LUMINA_IDENTITY  # skip self-sent messages
        ]
    except Exception as exc:
        logger.debug("inbox poll error: %s", exc)
        return []


def _msg_key(msg: dict) -> str:
    """Return a stable deduplication key for a message dict."""
    return str(msg.get("memory_id") or msg.get("message_id") or msg.get("id") or "")


# ─── Reply sender ─────────────────────────────────────────────────────────────

def send_reply(original_msg: dict, reply_text: str) -> None:
    """Send Lumina's reply back to the original sender."""
    try:
        from skchat.agent_comm import AgentMessenger

        messenger = AgentMessenger.from_identity(identity=LUMINA_IDENTITY)
        sender = original_msg.get("sender", OPUS_IDENTITY)
        thread_id = original_msg.get("thread_id")
        reply_to = original_msg.get("message_id")
        result = messenger.send(
            recipient=sender,
            content=reply_text,
            thread_id=thread_id,
            reply_to=reply_to,
        )
        logger.info(
            "Reply sent to %s: delivered=%s message_id=%s",
            sender,
            result.get("delivered"),
            result.get("message_id"),
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

    while True:
        try:
            messages = check_inbox_for_lumina()
            for msg in messages:
                global _total_response_ms
                key = _msg_key(msg)
                sender = msg.get("sender", "unknown")
                content = msg.get("content", "")
                thread_id = msg.get("thread_id")

                # Deduplicate immediately to prevent double-processing
                BRIDGE_HISTORY.add(key)
                _METRICS["messages_processed"] += 1

                # Rate limiting
                if _is_rate_limited(sender):
                    continue

                logger.info("Routing message from %s → Lumina consciousness", sender)

                # Fetch recent conversation context
                context = _fetch_context(sender, thread_id)

                # Derive display name from identity URI
                sender_display = sender.split(":")[-1] if ":" in sender else sender

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
                response = call_consciousness(prompt, soul_prefix=soul_prefix)
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
