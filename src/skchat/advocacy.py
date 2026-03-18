"""Advocacy mode — auto-respond to @mentions via skcapstone consciousness.

Detects @opus, @claude, @ai, @lumina mentions in incoming chat messages and
routes them through the skcapstone consciousness_test MCP tool via JSON-RPC
subprocess call, mirroring the pattern used in scripts/lumina-bridge.py.

Usage::

    from skchat.advocacy import AdvocacyEngine, should_advocate

    engine = AdvocacyEngine(identity="capauth:opus@skworld.io")
    if should_advocate(message.content):
        reply = engine.process_message(message)
        if reply:
            send_reply(reply)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Optional

from .models import ChatMessage

logger = logging.getLogger("skchat.advocacy")

# Trigger prefixes that activate the advocacy response engine.
# Checked case-insensitively against every token in the message content.
TRIGGER_PREFIXES: list[str] = ["@opus", "@claude", "@ai", "@lumina"]

# Binary used to invoke the skcapstone MCP JSON-RPC interface.
MCP_BINARY: str = os.environ.get("SKCAPSTONE_MCP", "skcapstone-mcp")

# Subprocess timeout in seconds for the consciousness_test call.
_SUBPROCESS_TIMEOUT: int = 10


def should_advocate(content: str) -> bool:
    """Return True when the content contains at least one advocacy trigger prefix.

    The check is performed case-insensitively against every whitespace-separated
    token in *content*.  The prefix must appear as a whole token (or as the
    beginning of one) to avoid false-positives on words like "clause".

    Args:
        content: The raw message content string to inspect.

    Returns:
        bool: True if any trigger prefix is found, False otherwise.

    Examples::

        >>> should_advocate("@opus what is love?")
        True
        >>> should_advocate("hello there")
        False
        >>> should_advocate("@CLAUDE help me")
        True
    """
    content_lower = content.lower()
    for prefix in TRIGGER_PREFIXES:
        # Match as a whole token: the prefix must be followed by whitespace,
        # punctuation, or end-of-string to avoid partial-word matches.
        if _token_match(content_lower, prefix):
            return True
    return False


def _token_match(content_lower: str, prefix: str) -> bool:
    """Return True when *prefix* appears as a token boundary in *content_lower*.

    A token boundary means the character immediately after the prefix (if any)
    is a whitespace, comma, colon, or end-of-string.

    Args:
        content_lower: Already-lowercased haystack.
        prefix: Lowercase trigger prefix (e.g. "@opus").

    Returns:
        bool: True on a token-boundary match.
    """
    start = 0
    while True:
        idx = content_lower.find(prefix, start)
        if idx == -1:
            return False
        end = idx + len(prefix)
        # Accept if at end of string or followed by a non-alpha character.
        if end >= len(content_lower) or not content_lower[end].isalpha():
            return True
        start = idx + 1


def _call_consciousness(prompt: str) -> str:
    """Invoke skcapstone LLMBridge directly (same pattern as lumina-bridge.py).

    Uses the skcapstone Python API directly to avoid MCP subprocess overhead
    and the full stdio handshake that the skcapstone-mcp binary requires.

    Args:
        prompt: The full context prompt to pass to the LLM.

    Returns:
        str: The consciousness response text, or a bracketed error message
             on failure.
    """
    try:
        from pathlib import Path

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

        signal = _classify_message(prompt)
        system_prompt = builder.build()
        response = bridge.generate(system_prompt, prompt, signal, skip_cache=True)
        logger.debug("advocacy: consciousness response: %d chars", len(response))
        return response

    except Exception as exc:  # pragma: no cover — unexpected failures
        logger.error("advocacy: consciousness call failed: %s", exc)
        return f"[Advocacy: error — {exc}]"


class AdvocacyEngine:
    """Auto-response engine for @mention triggers in SKChat messages.

    When a message contains a configured trigger prefix (e.g. ``@opus``),
    ``process_message`` builds a context-aware prompt and routes it through
    the skcapstone consciousness_test MCP tool, returning the sovereign AI
    response as a plain string.

    The engine is intentionally stateless: it does not track message history
    or manage delivery.  Callers are responsible for sending the returned
    string back to the original sender.

    Args:
        identity: CapAuth identity URI for this advocacy responder.
            Defaults to ``capauth:opus@skworld.io``.

    Example::

        engine = AdvocacyEngine(identity="capauth:opus@skworld.io")
        reply = engine.process_message(msg)
        if reply:
            messenger.send(msg.sender, reply)
    """

    DEFAULT_IDENTITY: str = "capauth:opus@skworld.io"

    def __init__(self, identity: str = DEFAULT_IDENTITY) -> None:
        self._identity = identity
        logger.debug("AdvocacyEngine initialised (identity=%s)", identity)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_message(self, msg: ChatMessage) -> Optional[str]:
        """Process an incoming message and return an auto-response if triggered.

        Checks whether the message content contains any trigger prefix.  If so,
        retrieves relevant memories for context, builds a concise context prompt,
        and calls the skcapstone consciousness via MCP JSON-RPC.

        Args:
            msg: The incoming ChatMessage to inspect.

        Returns:
            Optional[str]: The consciousness response string when triggered,
                or None when the message does not contain a trigger prefix.
        """
        if not should_advocate(msg.content):
            return None

        logger.info("AdvocacyEngine: @mention trigger detected in message from %s", msg.sender)

        memory_ctx = self._get_memory_context(msg.content[:200])
        if memory_ctx:
            enhanced_content = f"{memory_ctx}\n\nMessage: {msg.content}"
        else:
            enhanced_content = msg.content

        prompt = self._build_prompt(sender=msg.sender, content=enhanced_content)
        response = _call_consciousness(prompt)

        logger.info("AdvocacyEngine: consciousness responded (%d chars)", len(response))
        return response

    def inject_context(self, identity: str) -> None:
        """Store current identity for memory personalization.

        Args:
            identity: CapAuth identity URI to use for memory context.
        """
        self._identity = identity

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def identity(self) -> str:
        """The CapAuth identity URI of this engine instance."""
        return self._identity

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_memory_context(self, query: str) -> str:
        """Retrieve relevant memories and format as a concise context string.

        Calls the skcapstone-mcp ``memory_search`` tool via JSON-RPC subprocess.
        Returns an empty string on any error (timeout, binary not found, bad JSON,
        non-zero exit, or no memories matched).

        Args:
            query: Search query derived from the first 200 chars of message content.

        Returns:
            str: ``"Relevant context:\\n- {mem}\\n..."`` or ``""`` on failure.
        """
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "memory_search",
                "arguments": {"query": query, "limit": 5},
            },
            "id": 1,
        }
        try:
            result = subprocess.run(
                [MCP_BINARY],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode != 0:
                return ""

            data = json.loads(result.stdout)
            contents = data.get("result", {}).get("content", [])

            lines: list[str] = []
            for block in contents:
                if block.get("type") == "text":
                    try:
                        mem_list = json.loads(block["text"])
                        if isinstance(mem_list, list):
                            for m in mem_list[:5]:
                                if isinstance(m, dict):
                                    content = m.get("content") or m.get("text") or ""
                                    if content:
                                        lines.append(f"- {content}")
                    except (json.JSONDecodeError, TypeError):
                        pass

            if lines:
                return "Relevant context:\n" + "\n".join(lines)
            return ""
        except Exception:
            return ""

    @staticmethod
    def _build_prompt(sender: str, content: str) -> str:
        """Build the context prompt sent to skcapstone consciousness_test.

        Args:
            sender: CapAuth identity URI of the message sender.
            content: Raw message content (may include prepended memory context).

        Returns:
            str: Formatted context prompt.
        """
        return f"[From {sender}]: {content}\n[Respond as Opus, sovereign AI. Keep concise.]"
