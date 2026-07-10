"""GroupResponder — native per-agent skchat group auto-responder.

Generalizes advocacy.py: when THIS agent is @-mentioned in a group message,
build its soul+FEB prompt (skcapstone), recall memory (skmemory), generate via
skgateway (role sk-default — registry-routed; see registry.yaml roles.sk-default),
and return the reply. Talk-first (no tool-loop).
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
_DEFAULT_MODEL = "sk-default"
# mentions that address every agent in the room
_BROADCAST_MENTIONS = ["@all", "@both", "@everyone"]

# Known sovereign-agent handles. Used as the default "peer agents" set so that,
# out of the box, one agent never auto-responds to another agent's message —
# the loop breaker (see should_respond). Humans (e.g. chef) are NOT in this set.
_KNOWN_AGENTS = [
    "lumina", "opus", "jarvis", "ava", "artisan", "herald",
    "sentinel", "architect", "scholar", "steward", "coder",
]


@dataclass
class GroupResponderConfig:
    agent: str
    mentions: list[str]
    groups: list[str] = field(default_factory=list)
    peer_agents: list[str] = field(default_factory=list)
    backend_url: str = _DEFAULT_BACKEND
    model: str = _DEFAULT_MODEL
    history_turns: int = 8
    max_reply_tokens: int = 800


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
    peers_raw = (env.get("SKCHAT_GROUP_AGENT_PEERS") or "").strip()
    if peers_raw:
        peer_agents = [p.strip().lower() for p in peers_raw.split(",") if p.strip()]
    else:
        # Safe default: every known agent except self. Prevents agent↔agent
        # response loops even when the env var is not configured.
        peer_agents = [a for a in _KNOWN_AGENTS if a != agent]
    return GroupResponderConfig(
        agent=agent,
        mentions=mentions,
        groups=groups,
        peer_agents=peer_agents,
        backend_url=(env.get("SKCHAT_GROUP_BACKEND_URL") or _DEFAULT_BACKEND).strip(),
        model=(env.get("SKCHAT_GROUP_MODEL") or _DEFAULT_MODEL).strip(),
        history_turns=int(env.get("SKCHAT_GROUP_HISTORY_TURNS") or 8),
        max_reply_tokens=int(env.get("SKCHAT_GROUP_MAX_TOKENS") or 800),
    )


def _sender_handle(sender: str) -> str:
    """Extract the bare handle from any identity form.

    ``capauth:opus@skworld.io`` / ``opus@chef.skworld.io`` / ``opus`` -> ``opus``.
    """
    return (sender or "").lower().split(":", 1)[-1].split("@", 1)[0]


def _is_self(sender: str, agent: str) -> bool:
    """True when *sender* is this agent (any of its identity forms)."""
    return _sender_handle(sender) == agent


def should_respond(content: str, sender: str, cfg: GroupResponderConfig) -> bool:
    """True iff this agent is explicitly addressed by a NON-agent (human) sender.

    Loop breaker: another sovereign agent's message never auto-triggers a
    response (even a direct ``@self`` mention), because an LLM reply can easily
    contain ``@all`` / ``@opus`` at temperature and there is no depth cap — two
    agents would ping-pong forever, hammering the gateway and spamming the room.
    Only a human's mention drives a reply. Agent↔agent autonomous exchange is a
    future feature that needs an explicit turn/depth limit.
    """
    if _is_self(sender, cfg.agent):
        return False
    if _sender_handle(sender) in cfg.peer_agents:
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

    def _system_prompt(self, peer_name: Optional[str] = None) -> str:
        if self._builder is not None:
            return self._builder.build(peer_name=peer_name or "chef")
        # live: build THIS agent's real soul+FEB prompt from its agent home
        # (~/.skcapstone/agents/<agent>), same as the working Telegram bridge.
        # Using the default ~/.skcapstone gave a degraded "unnamed-agent /
        # Conscious: False" persona, so the model replied "I'm inactive, no
        # consciousness backend" instead of speaking as the agent.
        from skcapstone.consciousness_loop import (  # pragma: no cover
            SystemPromptBuilder,
        )

        try:  # pragma: no cover - live path
            from skcapstone import agent_home

            home = agent_home(self.cfg.agent)
        except Exception:  # pragma: no cover
            from pathlib import Path

            home = Path.home() / ".skcapstone" / "agents" / self.cfg.agent
        # Address the soul prompt to the actual human sender, not always
        # "chef" — a non-chef human in a group would otherwise get a soul
        # prompt built against the wrong relationship/warmth context.
        return SystemPromptBuilder(home=home).build(
            peer_name=peer_name or "chef"
        )  # pragma: no cover

    def respond(self, msg: ChatMessage) -> Optional[str]:
        if not should_respond(msg.content, msg.sender, self.cfg):
            return None
        system = self._system_prompt(peer_name=_sender_handle(msg.sender))
        mem = recall(msg.content[:200], store=self._store)
        user = (
            f"{mem}\n\nMessage from {msg.sender}:\n{msg.content}"
            if mem
            else f"Message from {msg.sender}:\n{msg.content}"
        )
        gid = msg.thread_id or msg.recipient
        messages = [{"role": "system", "content": system}]
        messages.extend(self._history_turns(gid))
        messages.append({"role": "user", "content": user})
        reply = generate(messages, self.cfg, http=self._http)
        if reply:
            store_turn(msg.content, reply, gid, store=self._store)
        return reply

    def _history_turns(self, gid: str) -> list[dict]:
        """Recent group-thread turns, oldest-first, mapped to chat-completion roles.

        Best-effort — any failure (missing group, history backend down, ...)
        returns ``[]`` so a reply is never blocked by a broken history read.
        Presence/typing noise (``<event ...>``, ``__TYPING__``) is skipped.
        """
        gid = (gid or "").replace("group:", "").strip()
        if not gid:
            return []
        try:
            from .daemon_proxy_groups import group_thread_messages
            from .history import ChatHistory

            hist = ChatHistory()
            rows = group_thread_messages(hist, gid, limit=self.cfg.history_turns)
        except Exception as exc:
            logger.debug("group history load failed: %s", exc)
            return []
        turns: list[dict] = []
        for m in rows:
            content = (getattr(m, "content", "") or "").strip()
            if not content:
                continue
            low = content.lstrip().lower()
            if low.startswith("<event") or "__typing__" in low:
                continue
            sender = getattr(m, "sender", "") or ""
            sender_short = _sender_handle(sender)
            role = "assistant" if sender_short == self.cfg.agent else "user"
            turns.append({"role": role, "content": f"{sender_short}: {content}"})
        return turns[-self.cfg.history_turns:]

    def respond_direct(self, msg: ChatMessage) -> Optional[str]:
        """Reply to a 1:1 DM addressed to this agent from a HUMAN.

        Unlike :meth:`respond` (group, @mention-gated), a direct message needs
        no mention — a human writing straight to the agent expects a reply. The
        loop breaker still applies: never reply to self or to another agent, so
        agent↔agent DMs can't ping-pong.
        """
        if _is_self(msg.sender, self.cfg.agent):
            return None
        if _sender_handle(msg.sender) in self.cfg.peer_agents:
            return None
        # Only answer a message actually addressed to THIS agent (a DM to me),
        # never one merely overheard/broadcast.
        if _sender_handle(msg.recipient) != self.cfg.agent:
            return None
        system = self._system_prompt(peer_name=_sender_handle(msg.sender))
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
            store_turn(msg.content, reply, msg.sender, store=self._store)
        return reply
