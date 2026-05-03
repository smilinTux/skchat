"""Shared chat-context fetcher for SKChat bridges and the webui.

Historically each bridge (lumina-bridge.py, opus-bridge.py) had its own
``_fetch_context()`` that filtered by ``(self_identity, sender)`` pair.
That breaks group threads: when Jarvis posts in a group thread that
Lumina also belongs to, Lumina's context never sees Jarvis's message
because the pair filter rejects it.

This module centralizes the logic: for *threaded* messages we return all
messages on the thread (the group lens), for 1:1 DMs we keep the pair
filter (the DM lens).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("skchat.context")

DEFAULT_CONTEXT_MESSAGES = 5


def _looks_like_group_recipient(uri: str) -> bool:
    """Heuristic: is this a group/thread URI rather than an individual?

    Group IDs in skchat are bare UUID v4 strings (no scheme prefix); any
    "group:..." URI also counts. Individual identities use ``capauth:``,
    ``did:``, etc.
    """
    if not uri:
        return False
    if uri.startswith("group:"):
        return True
    if ":" in uri:
        return False  # has a scheme → individual identity
    # Bare UUIDish: 32 hex + 4 dashes
    return len(uri) == 36 and uri.count("-") == 4


def _format_message(
    msg: dict[str, Any],
    self_identity: str,
    is_group: bool,
) -> Optional[str]:
    """Render a single history dict as one context line.

    Returns None for malformed entries (missing sender or content).
    Group messages get a `[name → group]` arrow so the bridge can see
    the inter-agent message rather than mistaking it for a DM.
    """
    sender = msg.get("sender") or ""
    content = msg.get("content") or ""
    if not sender or not content:
        return None

    recipient = msg.get("recipient") or ""
    display = sender.split(":")[-1] if ":" in sender else sender
    if "@" in display:
        display = display.split("@", 1)[0]

    if is_group and recipient and recipient != self_identity:
        arrow = " → group" if _looks_like_group_recipient(recipient) else ""
        return f"[{display}{arrow}]: {content}"
    return f"[{display}]: {content}"


def fetch_context(
    self_identity: str,
    sender: str,
    thread_id: Optional[str] = None,
    *,
    limit: int = DEFAULT_CONTEXT_MESSAGES,
    history: Optional[Any] = None,
) -> str:
    """Fetch recent conversation context, group-aware.

    For threaded messages we union together:
      - ``history.get_thread_messages(thread_id)`` — the SKMemory tag
        index. Returns per-member copies; we deduplicate by
        ``chat_message_id`` so a 4-member group doesn't render the same
        line four times.
      - ``history.get_thread(thread_id)`` — the JSONL backing store.
        Catches messages that were saved to JSONL but not yet indexed
        into SKMemory (e.g. file-transport loopbacks).

    For 1:1 DMs we fall back to the pair-filtered ``get_conversation`` —
    that's the right lens for "what have these two been talking about".

    Args:
        self_identity: CapAuth URI of the agent calling for context.
        sender: CapAuth URI of the message that triggered this fetch.
        thread_id: Thread/group identifier, if any.
        limit: Max lines to return.
        history: Optional ChatHistory instance — useful for tests.

    Returns:
        Multi-line string, oldest first, or empty string on any failure.
    """
    try:
        if history is None:
            from skchat.history import ChatHistory

            history = ChatHistory.from_config()

        is_group = bool(thread_id) or _looks_like_group_recipient(sender)
        messages: list[dict[str, Any]] = []

        if thread_id:
            # Tag-indexed copies (SQLite). Already returns dicts.
            try:
                tagged = history.get_thread_messages(thread_id, limit=limit * 4)
            except Exception as exc:
                logger.debug("get_thread_messages failed: %s", exc)
                tagged = []

            # JSONL-backed scan — catches messages that bypass SKMemory
            # indexing (file-transport loopback, daemon-down recovery).
            try:
                jsonl = history.get_thread(thread_id, limit=limit * 4)
            except Exception as exc:
                logger.debug("get_thread (JSONL) failed: %s", exc)
                jsonl = []

            # Normalize JSONL ChatMessage objects → dicts so dedup works.
            jsonl_dicts: list[dict[str, Any]] = []
            for m in jsonl:
                if hasattr(m, "model_dump"):
                    jsonl_dicts.append(
                        {
                            "chat_message_id": getattr(m, "id", None),
                            "sender": getattr(m, "sender", ""),
                            "recipient": getattr(m, "recipient", ""),
                            "content": getattr(m, "content", ""),
                            "timestamp": getattr(m, "timestamp", None),
                            "thread_id": getattr(m, "thread_id", None),
                        }
                    )

            # Merge + dedupe by chat_message_id (fall back to (sender, content)
            # when the SKMemory copy lacks an id).
            seen: set[str] = set()
            for src in (tagged, jsonl_dicts):
                for m in src:
                    mid = m.get("chat_message_id") or m.get("id")
                    key = mid or f"{m.get('sender')}|{m.get('content')}"
                    if key in seen:
                        continue
                    seen.add(key)
                    messages.append(m)
        else:
            # 1:1 DM — pair filter is the right lens. Try the SKMemory
            # tag-indexed lookup first; fall back to the JSONL backing
            # store filtered by peer when no SKMemory store is present
            # (test environments, file-only deployments).
            try:
                messages = history.get_conversation(
                    self_identity, sender, limit=limit * 2
                )
            except Exception as exc:
                logger.debug("get_conversation failed: %s", exc)
                messages = []

            if not messages:
                try:
                    raw = history.load(peer=sender, limit=limit * 4)
                    pair: list[dict[str, Any]] = []
                    for m in raw:
                        s = getattr(m, "sender", "") or ""
                        r = getattr(m, "recipient", "") or ""
                        # Only true 1:1 between self and sender.
                        if {s, r} == {self_identity, sender}:
                            pair.append(
                                {
                                    "chat_message_id": getattr(m, "id", None),
                                    "sender": s,
                                    "recipient": r,
                                    "content": getattr(m, "content", ""),
                                    "timestamp": getattr(m, "timestamp", None),
                                    "thread_id": getattr(m, "thread_id", None),
                                }
                            )
                    messages = pair
                except Exception as exc:
                    logger.debug("history.load(peer=...) fallback failed: %s", exc)

        if not messages:
            return ""

        # Oldest-first chronological ordering.
        messages.sort(key=lambda d: d.get("timestamp") or "")

        # Trim to the requested window from the END (most recent N lines).
        windowed = messages[-limit:]

        lines: list[str] = []
        for m in windowed:
            line = _format_message(m, self_identity, is_group)
            if line:
                lines.append(line)
        return "\n".join(lines)

    except Exception as exc:
        logger.debug("fetch_context failed: %s", exc)
        return ""
