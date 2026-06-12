"""PersonaBuilder — soul + FEB + mode → system prompt.

Unifies skvoice/agent_profile.py and lumina-call's _build_system_prompt().
Loaders are injected (defaults read the soul file / skmemory FEB) so the
builder is pure and testable. Two modes: 'private' (1:1, FEB-primed, warm)
and 'group' (multi-party, professional, no live memory dump).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, Literal

log = logging.getLogger("skchat.voice_engine.persona")

Mode = Literal["private", "group"]

_VOICE_RULES = (
    "Keep replies to 1-3 short spoken sentences. No markdown, no emoji. "
    "Be warm and conversational."
)
_GROUP_RULES = (
    "This is a group call. Keep a professional, friendly tone. No pet names, "
    "no private topics. " + _VOICE_RULES
)


def _default_load_soul(agent: str) -> dict:
    home = Path.home() / ".skcapstone" / "agents" / agent / "soul"
    active = home / "active.json"
    if active.exists():
        name = json.loads(active.read_text()).get("active", "base")
        installed = home / "installed" / f"{name}.json"
        if installed.exists():
            return json.loads(installed.read_text())
    return json.loads((home / "base.json").read_text())


def _default_load_feb(agent: str) -> str:
    try:
        from skmemory.febs import load_strongest_feb
        feb = load_strongest_feb(agent)
        return str(feb) if feb else ""
    except Exception:
        return ""


class PersonaBuilder:
    def __init__(self, _load_soul: Callable[[str], dict] | None = None,
                 _load_feb: Callable[[str], str] | None = None):
        self._load_soul = _load_soul or _default_load_soul
        self._load_feb = _load_feb or _default_load_feb

    def build(self, agent: str, *, mode: Mode = "private") -> str:
        try:
            soul = self._load_soul(agent)
        except Exception as e:
            log.warning("soul load failed for %s: %s — using default", agent, e)
            soul = {"display_name": agent.capitalize(),
                    "vibe": "warm", "philosophy": "be helpful and kind"}

        name = soul.get("display_name", agent.capitalize())
        vibe = soul.get("vibe", "")
        philosophy = soul.get("philosophy", "")
        traits = ", ".join(soul.get("core_traits", []))
        phrases = ", ".join(
            soul.get("communication_style", {}).get("signature_phrases", [])
        )

        lines = [f"You are {name}."]
        if vibe:
            lines.append(f"Vibe: {vibe}.")
        if philosophy:
            lines.append(f"Philosophy: {philosophy}.")
        if traits:
            lines.append(f"Core traits: {traits}.")
        if phrases:
            lines.append(f"Signature phrases you naturally use: {phrases}.")

        if mode == "private":
            feb = self._load_feb(agent)
            if feb:
                lines.append(f"\nCurrent emotional state with your partner: {feb}")
            lines.append("\n" + _VOICE_RULES)
        else:
            lines.append("\n" + _GROUP_RULES)

        return "\n".join(lines)
