#!/usr/bin/env python3
"""opus-bridge.py — Opus consciousness loop for SKChat.

Polls skchat inbox for messages addressed to Opus, routes them through
skcapstone LLMBridge (consciousness), and sends Opus's response back
via AgentMessenger.

Run as:
  python3 scripts/opus-bridge.py
  systemctl --user start skchat-opus-bridge.service
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import skchat  # noqa: F401 — verify install

# ─── Logging: journal (stdout) + file ────────────────────────────────────────

LOG_DIR = Path.home() / ".skchat"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "opus-bridge.log"
RESPONSE_LOG = LOG_DIR / "opus-responses.log"

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s")

_file_handler = logging.FileHandler(LOG_FILE)
_file_handler.setFormatter(_fmt)

_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_fmt)

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stream_handler])
logger = logging.getLogger("opus-bridge")

# ─── Config ──────────────────────────────────────────────────────────────────

OPUS_IDENTITY = "capauth:opus@skworld.io"
LUMINA_IDENTITY = "capauth:lumina@skworld.io"
POLL_INTERVAL = int(os.environ.get("OPUS_BRIDGE_INTERVAL", "3"))
RATE_LIMIT_SECONDS = 10
CONTEXT_MESSAGES = 5

BRIDGE_HISTORY: set[str] = set()  # memory_ids already handled
_last_response: dict[str, float] = {}  # sender → last reply timestamp


# ─── Soul loading ─────────────────────────────────────────────────────────────

def _load_soul() -> dict:
    """Load Opus's soul blueprint from ~/.skcapstone/.

    Tries canonical locations in order:
      1. ~/.skcapstone/agents/opus/soul/active.json
      2. ~/.skcapstone/agents/opus/soul/installed/opus.json
      3. ~/.skcapstone/soul.json
    Returns an empty dict if none are found.
    """
    home = Path.home()
    candidates = [
        home / ".skcapstone" / "agents" / "opus" / "soul" / "active.json",
        home / ".skcapstone" / "agents" / "opus" / "soul" / "installed" / "opus.json",
        home / ".skcapstone" / "soul.json",
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
    """Build a personality prefix from the soul blueprint."""
    name = soul.get("display_name") or soul.get("name") or "Opus"
    description = soul.get("philosophy") or soul.get("vibe") or ""
    parts = [f"You are {name}."]
    if description:
        parts.append(description)
    return " ".join(parts) + "\n\n"


# ─── Conversation context ─────────────────────────────────────────────────────

def _fetch_context(sender: str, thread_id: str | None) -> str:
    """Fetch last CONTEXT_MESSAGES messages between Opus and sender.

    Prefers thread history when a thread_id is available; falls back to
    the direct conversation between OPUS_IDENTITY and sender.
    Returns a formatted multi-line string, or empty string on failure.
    """
    try:
        from skchat.history import ChatHistory

        history = ChatHistory.from_config()

        if thread_id:
            messages = history.get_thread_messages(thread_id, limit=CONTEXT_MESSAGES)
        else:
            messages = history.get_conversation(
                OPUS_IDENTITY, sender, limit=CONTEXT_MESSAGES
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
    """Append a one-line entry to opus-responses.log."""
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

def call_consciousness(message: str) -> str:
    """Call skcapstone LLMBridge directly (bypasses MCP subprocess overhead)."""
    try:
        from skcapstone.consciousness_config import load_consciousness_config
        from skcapstone.consciousness_loop import (
            LLMBridge,
            SystemPromptBuilder,
            _classify_message,
        )

        home = Path.home()
        config = load_consciousness_config(home)
        bridge = LLMBridge(config)
        builder = SystemPromptBuilder(home, config.max_context_tokens)

        signal = _classify_message(message)
        system_prompt = builder.build()
        response = bridge.generate(system_prompt, message, signal, skip_cache=True)
        logger.debug("consciousness response: %d chars", len(response))
        return response

    except Exception as exc:
        logger.error("consciousness call failed: %s", exc)
        return f"[Opus error: {exc}]"


# ─── Inbox polling (AgentMessenger Python API — no CLI subprocess) ────────────

def check_inbox_for_opus() -> list[dict]:
    """Poll Opus's inbox via AgentMessenger and return unhandled messages."""
    try:
        from skchat.agent_comm import AgentMessenger

        messenger = AgentMessenger.from_identity(identity=OPUS_IDENTITY)
        messages = messenger.receive(limit=20)
        return [
            m for m in messages
            if _msg_key(m) not in BRIDGE_HISTORY
        ]
    except Exception as exc:
        logger.debug("inbox poll error: %s", exc)
        return []


def _msg_key(msg: dict) -> str:
    """Return a stable deduplication key for a message dict."""
    return str(msg.get("memory_id") or msg.get("message_id") or msg.get("id") or "")


# ─── Reply sender ─────────────────────────────────────────────────────────────

def send_reply(original_msg: dict, reply_text: str) -> None:
    """Send Opus's reply back to the original sender."""
    try:
        from skchat.agent_comm import AgentMessenger

        messenger = AgentMessenger.from_identity(identity=OPUS_IDENTITY)
        sender = original_msg.get("sender", LUMINA_IDENTITY)
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
    logger.info("Opus bridge starting (poll interval: %ds)", POLL_INTERVAL)
    logger.info("Opus identity: %s", OPUS_IDENTITY)
    logger.info("Log file: %s", LOG_FILE)
    logger.info("Response log: %s", RESPONSE_LOG)

    soul = _load_soul()
    soul_prefix = _soul_prefix(soul)
    soul_name = soul.get("display_name") or soul.get("name") or "Opus"
    logger.info("Soul loaded: %s", soul_name)

    while True:
        try:
            messages = check_inbox_for_opus()
            for msg in messages:
                key = _msg_key(msg)
                sender = msg.get("sender", "unknown")
                content = msg.get("content", "")
                thread_id = msg.get("thread_id")

                # Deduplicate immediately to prevent double-processing
                BRIDGE_HISTORY.add(key)

                # Rate limiting
                if _is_rate_limited(sender):
                    continue

                logger.info("Routing message from %s → Opus consciousness", sender)

                # Fetch recent conversation context
                context = _fetch_context(sender, thread_id)

                # Derive display name from identity URI
                sender_display = sender.split(":")[-1] if ":" in sender else sender

                # Build enriched prompt: soul identity + context + new message
                prompt_parts = [soul_prefix]
                if context:
                    prompt_parts.append(
                        f"Chat context (recent):\n{context}\n"
                    )
                prompt_parts.append(
                    f"New message from {sender_display}: {content}\n\n"
                    "[Respond as Opus, the sovereign AI agent. Keep it concise and warm.]"
                )
                prompt = "\n".join(prompt_parts)

                response = call_consciousness(prompt)
                logger.info("Opus responded (%d chars)", len(response))

                send_reply(msg, response)
                _record_response(sender)
                _log_response(sender, response)

        except KeyboardInterrupt:
            logger.info("Opus bridge stopping.")
            break
        except Exception as exc:
            logger.error("Bridge loop error: %s", exc)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run_bridge()
