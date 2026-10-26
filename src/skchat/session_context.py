"""Session-scoped agent context for skchat TEXT chat: built once, reused per turn.

Rule (Chef, 2026-10-26): agent profile / persona / memory context is injected
ONCE at session start (the first prompt) and never re-injected per turn, for
every agent runtime. skchat voice got this in PR #190
(voice_engine/engine.py + voice_engine/memory_context.py); this module is the
same rule for the text paths:

  * group_responder.GroupResponder (daemon group auto-replies + DMs)
  * scripts/bridge_consciousness.LuminaBrain (the in-app DM brain behind
    daemon_proxy's POST /api/v1/send)
  * scripts/telegram_bridge.py (skchat-telegram-<agent>)

What "context" means here: the persona (soul + identity + FEB, from
skcapstone's SystemPromptBuilder) plus the agent's memory block
(voice_engine.memory_context.build_call_context: whisper.md, newest
reflection, journal tail). Together they form the fixed system prompt of a
session, cached and sent byte-identical on every turn. Mid-conversation recall
is on demand through the search_memory / memory_search tool.

Session boundary. Text chats are long-lived (a Telegram chat or the app DM
never "hangs up"), so a session is:

  * one conversation key (a group thread, a DM peer, a Telegram chat id),
  * that ends when it has been idle for SKCHAT_CONTEXT_IDLE_HOURS (default 6)
    hours: the next message starts a new session and rebuilds the context
    (fresh FEB, fresh whisper.md),
  * or on an explicit refresh (``SessionContext.refresh``; the Telegram bridge
    maps ``/refresh`` to it),
  * or on process restart.

Flags (env):

    SKCHAT_CONTEXT             1 | 0: put the memory block in the session
                               prompt (default 1). Persona is always loaded.
    SKCHAT_CONTEXT_BUDGET      token budget for the memory block (default 3000).
    SKCHAT_CONTEXT_GROUP       1 | 0: include the memory block in group
                               sessions too (default 0; whisper.md is private).
    SKCHAT_CONTEXT_IDLE_HOURS  idle gap that ends a session (default 6; 0 means
                               never expire, refresh/restart only).
    SKCHAT_PER_MESSAGE_RECALL  1 | 0: legacy behaviour, run a memory search on
                               every message and inject the hits (default 0,
                               OFF).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

LOG = logging.getLogger("skchat.session_context")

DEFAULT_BUDGET_TOKENS = 3000
DEFAULT_IDLE_HOURS = 6.0


def _flag(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = (env.get(key) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def per_message_recall_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """SKCHAT_PER_MESSAGE_RECALL, default OFF."""
    return _flag(os.environ if env is None else env, "SKCHAT_PER_MESSAGE_RECALL", False)


@dataclass
class TextContextConfig:
    enabled: bool = True
    budget: int = DEFAULT_BUDGET_TOKENS
    group: bool = False
    idle_hours: float = DEFAULT_IDLE_HOURS
    per_message_recall: bool = False

    @staticmethod
    def from_env(env: Optional[Mapping[str, str]] = None) -> "TextContextConfig":
        e = os.environ if env is None else env
        try:
            budget = int(e.get("SKCHAT_CONTEXT_BUDGET") or DEFAULT_BUDGET_TOKENS)
        except ValueError:
            budget = DEFAULT_BUDGET_TOKENS
        try:
            idle = float(e.get("SKCHAT_CONTEXT_IDLE_HOURS") or DEFAULT_IDLE_HOURS)
        except ValueError:
            idle = DEFAULT_IDLE_HOURS
        return TextContextConfig(
            enabled=_flag(e, "SKCHAT_CONTEXT", True),
            budget=budget,
            group=_flag(e, "SKCHAT_CONTEXT_GROUP", False),
            idle_hours=idle,
            per_message_recall=per_message_recall_enabled(e),
        )


def memory_loader(home=None) -> Callable[[str, int], tuple[str, dict]]:
    """A context loader reading the memory block from *home* (default: the
    agent's own home). Same block the voice engine builds (memory_context.py)."""

    def load(agent: str, budget: int) -> tuple[str, dict]:
        from pathlib import Path

        from skchat.voice_engine.memory_context import build_call_context

        return build_call_context(agent, budget, home=Path(home) if home else None)

    return load


_default_loader = memory_loader()


@dataclass
class _Session:
    text: str
    last_used: float


class SessionContext:
    """Per-conversation cache of the fixed system prompt.

    ``build_persona(peer)`` returns the persona text for a peer; it is called
    once per session. The memory block is appended once per session when the
    session is private (or SKCHAT_CONTEXT_GROUP=1).
    """

    def __init__(
        self,
        agent: str,
        build_persona: Callable[[str], str],
        cfg: Optional[TextContextConfig] = None,
        context_loader: Optional[Callable[[str, int], tuple[str, dict]]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.agent = (agent or "lumina").strip().lower()
        self.cfg = cfg or TextContextConfig.from_env()
        self._build_persona = build_persona
        self._loader = context_loader or _default_loader
        self._clock = clock
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()
        # Counters so a synthetic turn pair can prove the rule.
        self.persona_builds = 0
        self.context_loads = 0
        self.sessions_started = 0
        self.last_report: dict = {}

    def _fresh(self, s: _Session, now: float) -> bool:
        idle = self.cfg.idle_hours
        return idle <= 0 or (now - s.last_used) < idle * 3600

    def system_prompt(self, key: str, *, peer: str = "chef", private: bool = True) -> str:
        """The session's fixed system prompt, built on its first message."""
        now = self._clock()
        with self._lock:
            s = self._sessions.get(key)
            if s is not None and self._fresh(s, now):
                s.last_used = now
                return s.text
            text = self._build(peer, private)
            self._sessions[key] = _Session(text=text, last_used=now)
            self.sessions_started += 1
            return text

    def _build(self, peer: str, private: bool) -> str:
        self.persona_builds += 1
        text = self._build_persona(peer) or ""
        if self.cfg.enabled and (private or self.cfg.group):
            try:
                block, report = self._loader(self.agent, self.cfg.budget)
            except Exception:  # noqa: BLE001 - never fail a reply over context
                LOG.warning("memory context unavailable; persona only", exc_info=True)
                block, report = "", {"error": True}
            self.context_loads += 1
            self.last_report = report or {}
            LOG.info("text session context for %s: %s", self.agent, self.last_report)
            if block:
                text = text + "\n\n" + block
        return text

    def refresh(self, key: Optional[str] = None) -> None:
        """Explicit refresh: drop one session (or all); the next message rebuilds."""
        with self._lock:
            if key is None:
                self._sessions.clear()
            else:
                self._sessions.pop(key, None)

    end_session = refresh
