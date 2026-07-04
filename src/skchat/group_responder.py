"""GroupResponder — native per-agent skchat group auto-responder.

Generalizes advocacy.py: when THIS agent is @-mentioned in a group message,
build its soul+FEB prompt (skcapstone), recall memory (skmemory), generate via
skgateway (reg:ornith), and return the reply. Talk-first (no tool-loop).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Mapping, Optional

from .advocacy import _token_match
from .models import ChatMessage

logger = logging.getLogger("skchat.group_responder")

_DEFAULT_BACKEND = "http://localhost:18780/v1/chat/completions"
_DEFAULT_MODEL = "reg:ornith"
# mentions that address every agent in the room
_BROADCAST_MENTIONS = ["@all", "@both", "@everyone"]


@dataclass
class GroupResponderConfig:
    agent: str
    mentions: list[str]
    groups: list[str] = field(default_factory=list)
    backend_url: str = _DEFAULT_BACKEND
    model: str = _DEFAULT_MODEL
    history_turns: int = 8
    max_reply_tokens: int = 800
    on_error: str = "silent"  # "silent" | "note"


def load_group_config(
    agent: str, env: Optional[Mapping[str, str]] = None
) -> GroupResponderConfig:
    """Build a config for *agent* from env (SKCHAT_GROUP_*) with SKWorld defaults."""
    if env is None:
        env = os.environ
    agent = (agent or "lumina").strip().lower()
    mentions = [f"@{agent}"] + _BROADCAST_MENTIONS
    groups_raw = (env.get("SKCHAT_GROUPS") or "").strip()
    groups = [g.strip() for g in groups_raw.split(",") if g.strip()]
    return GroupResponderConfig(
        agent=agent,
        mentions=mentions,
        groups=groups,
        backend_url=(env.get("SKCHAT_GROUP_BACKEND_URL") or _DEFAULT_BACKEND).strip(),
        model=(env.get("SKCHAT_GROUP_MODEL") or _DEFAULT_MODEL).strip(),
        history_turns=int(env.get("SKCHAT_GROUP_HISTORY_TURNS") or 8),
        max_reply_tokens=int(env.get("SKCHAT_GROUP_MAX_TOKENS") or 800),
        on_error=(env.get("SKCHAT_GROUP_ON_ERROR") or "silent").strip(),
    )


def _is_self(sender: str, agent: str) -> bool:
    """True when *sender* is this agent (any of its identity forms)."""
    s = (sender or "").lower()
    # matches capauth:opus@skworld.io, opus@chef.skworld.io, opus, etc.
    handle = s.split(":", 1)[-1].split("@", 1)[0]
    return handle == agent


def should_respond(content: str, sender: str, cfg: GroupResponderConfig) -> bool:
    """True iff this agent is explicitly addressed and the sender is not itself."""
    if _is_self(sender, cfg.agent):
        return False
    low = (content or "").lower()
    return any(_token_match(low, m) for m in cfg.mentions)


def generate(
    messages: list[dict], cfg: GroupResponderConfig, http=None
) -> Optional[str]:
    """POST an OpenAI-shaped chat completion to skgateway; return the reply text."""
    if http is None:  # pragma: no cover - real client, exercised live
        import httpx
        http = httpx.Client()
    payload = {
        "model": cfg.model,
        "messages": messages,
        "max_tokens": cfg.max_reply_tokens,
        "temperature": 0.8,
    }
    try:
        resp = http.post(cfg.backend_url, json=payload, timeout=120.0)
        if resp.status_code >= 400:
            logger.warning("group generate: skgateway HTTP %s", resp.status_code)
            return None
        data = resp.json() or {}
        return (data.get("choices") or [{}])[0].get("message", {}).get("content")
    except Exception as exc:
        logger.warning("group generate failed: %s", exc)
        return None


def _default_store():  # pragma: no cover - live skmemory
    from skmemory import MemoryStore
    return MemoryStore()


def recall(query: str, store=None, limit: int = 5) -> str:
    """Return a short 'Relevant memories' block for *query*, or '' on any failure."""
    if not (query or "").strip():
        return ""
    try:
        store = store or _default_store()
        hits = store.search(query, limit=limit)
    except Exception as exc:
        logger.debug("group recall failed: %s", exc)
        return ""
    lines = []
    for m in hits or []:
        c = (getattr(m, "content", "") or "")[:240]
        if c:
            lines.append(f"- {c}")
    return ("Relevant memories:\n" + "\n".join(lines)) if lines else ""


def store_turn(user_text: str, reply: str, gid: str, store=None) -> None:
    """Best-effort: snapshot the exchange to skmemory tagged with the group."""
    try:
        store = store or _default_store()
        title = (user_text or reply or "group turn").strip()[:60]
        content = f"User: {user_text}\n\nReply: {reply}".strip()[:4000]
        store.snapshot(
            title=title, content=content,
            tags=["skchat", f"{gid}"], source="skchat", source_ref=gid,
        )
    except Exception as exc:
        logger.debug("group store_turn failed: %s", exc)


class GroupResponder:
    """Per-agent group auto-responder: mention -> soul prompt -> recall -> generate."""

    def __init__(
        self,
        cfg: GroupResponderConfig,
        *,
        prompt_builder=None,
        http=None,
        store=None,
    ):
        self.cfg = cfg
        self._builder = prompt_builder
        self._http = http
        self._store = store

    def _system_prompt(self) -> str:
        if self._builder is not None:
            return self._builder.build()
        # live: skcapstone soul+FEB builder (same as advocacy._call_consciousness)
        from pathlib import Path  # pragma: no cover - live path
        from skcapstone.consciousness_config import (  # pragma: no cover
            load_consciousness_config,
        )
        from skcapstone.consciousness_loop import (  # pragma: no cover
            SystemPromptBuilder,
        )
        home = Path.home()  # pragma: no cover
        config = load_consciousness_config(home)  # pragma: no cover
        return SystemPromptBuilder(  # pragma: no cover
            home, config.max_context_tokens
        ).build()  # pragma: no cover

    def respond(self, msg: ChatMessage) -> Optional[str]:
        if not should_respond(msg.content, msg.sender, self.cfg):
            return None
        system = self._system_prompt()
        mem = recall(msg.content[:200], store=self._store)
        user = (
            f"{mem}\n\nMessage from {msg.sender}:\n{msg.content}"
            if mem
            else f"Message from {msg.sender}:\n{msg.content}"
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        reply = generate(messages, self.cfg, http=self._http)
        if reply:
            gid = msg.thread_id or msg.recipient
            store_turn(msg.content, reply, gid, store=self._store)
        return reply
