"""GroupResponder — native per-agent skchat group auto-responder.

Generalizes advocacy.py: when THIS agent is @-mentioned in a group message,
build its soul+FEB prompt (skcapstone), recall memory (skmemory), generate via
skgateway (reg:ornith), and return the reply. Talk-first (no tool-loop).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional

from .advocacy import _token_match

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
