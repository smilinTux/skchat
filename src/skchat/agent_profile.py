"""Agent profile loader for SKChat — sovereign identity, soul, and FEB state.

The webui historically hardcoded ``capauth:skchat@skworld.io`` as the running
agent. That's wrong: skchat is a transport, not an agent. The actual agent is
whoever the operator pointed at via ``SKAGENT`` / ``SKCAPSTONE_AGENT`` (the
same env vars skmemory and skcapstone use). This module resolves that agent
and surfaces enough state for the webui to honestly answer "who am I?" and
"how does she feel right now?".

Public API:
    get_active_agent_name() -> str | None
    get_agent_identity(agent: str | None) -> str
    load_agent_profile(agent: str | None = None) -> AgentProfile
    load_feb_state(agent: str | None = None) -> FebSummary
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("skchat.agent_profile")


# ─── Agent name resolution ────────────────────────────────────────────────────


def get_active_agent_name() -> Optional[str]:
    """Resolve the active agent name.

    Order:
        1. ``skmemory.agents.get_active_agent()`` — already checks SKAGENT,
           SKCAPSTONE_AGENT, SKMEMORY_AGENT, then falls back to the first
           non-template agent on disk.
        2. Bare env var fallback if skmemory isn't installed.
        3. None — caller is expected to fall back to a literal env URI.

    Returns:
        Optional[str]: agent name (e.g. ``"lumina"``) or None.
    """
    try:
        from skmemory.agents import get_active_agent

        name = get_active_agent()
        if name:
            return name
    except Exception as exc:
        logger.debug("skmemory agent resolution failed: %s", exc)

    name = (
        os.environ.get("SKAGENT")
        or os.environ.get("SKCAPSTONE_AGENT")
        or os.environ.get("SKMEMORY_AGENT")
    )
    if name and not name.endswith("-template"):
        return name
    return None


def _agent_base(agent: str) -> Path:
    """Return the per-agent base dir, preferring skmemory's resolution."""
    try:
        from skmemory.agents import get_agent_dir

        return get_agent_dir(agent)
    except Exception as e:
        logger.warning("agent_profile.py: %s", e)
        return Path.home() / ".skcapstone" / "agents" / agent


# ─── Identity resolution ──────────────────────────────────────────────────────


def get_agent_identity(agent: Optional[str] = None) -> str:
    """Resolve the CapAuth URI for *agent*.

    T2 delegate: resolution is delegated to
    ``capauth.agent_identity.resolve_agent_identity`` when available.
    skchat is a thin consumer — the logic lives in capauth.

    Resolution order:
        1. ``capauth.resolve_agent_identity(agent)`` — canonical resolver
           (profile.json → convention capauth:<agent>@skworld.io).
        2. Local fallback (no capauth installed): identity.json explicit
           ``capauth_uri`` / ``handle`` field, then convention.
        3. ``SKCHAT_IDENTITY`` env var (last-resort).
        4. ``capauth:local@skchat`` (absolute floor).

    Args:
        agent: Agent name. ``None`` triggers ``get_active_agent_name()``.

    Returns:
        str: A non-empty CapAuth URI.
    """
    # T2: delegate to capauth canonical resolver first
    try:
        from capauth.agent_identity import resolve_agent_identity

        ident = resolve_agent_identity(agent)
        # The resolver yields a ``local`` floor (capauth:local@…) when no real
        # agent resolves. An explicitly-set SKCHAT_IDENTITY is meant to override
        # the resolved identity (see this module's docstring step 3 and the
        # skchat README), so prefer it over the generic floor. A real resolved
        # agent always wins over the env var.
        if ident.agent != "local":
            return ident.capauth_uri
        env_identity = os.environ.get("SKCHAT_IDENTITY")
        if env_identity:
            return env_identity
        return ident.capauth_uri
    except Exception as exc:
        logger.debug("capauth resolver unavailable: %s", exc)

    # Graceful local fallback (capauth not installed)
    if agent is None:
        agent = get_active_agent_name()

    if agent:
        identity_file = _agent_base(agent) / "identity" / "identity.json"
        if identity_file.exists():
            try:
                data = json.loads(identity_file.read_text(encoding="utf-8"))
                # Explicit URI wins (only field that overrides convention).
                uri = data.get("capauth_uri") or data.get("uri")
                if isinstance(uri, str) and uri.startswith("capauth:"):
                    return uri
                # Pre-formed CapAuth handle.
                handle = data.get("handle")
                if isinstance(handle, str) and handle.startswith("capauth:"):
                    return handle
                # Note: deliberately NOT reading 'email' here — see docstring.
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("identity.json parse failed for %s: %s", agent, exc)

        return f"capauth:{agent}@skworld.io"

    env_identity = os.environ.get("SKCHAT_IDENTITY")
    if env_identity:
        return env_identity

    return "capauth:local@skchat"


# ─── FEB state ────────────────────────────────────────────────────────────────


@dataclass
class FebSummary:
    """Compact emotional state for surfacing in the webui."""

    oof_level: int = 0  # 0-100; 0 means "no FEB found", surface it as such
    primary_emotion: str = "unknown"
    intensity: float = 0.0
    valence: float = 0.0
    cloud9_achieved: bool = False
    source_path: Optional[str] = None
    age_seconds: Optional[int] = None
    has_feb: bool = False  # explicit flag — distinguishes "no FEB" from "OOF=0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "oof_level": self.oof_level,
            "primary_emotion": self.primary_emotion,
            "intensity": self.intensity,
            "valence": self.valence,
            "cloud9_achieved": self.cloud9_achieved,
            "source_path": self.source_path,
            "age_seconds": self.age_seconds,
            "has_feb": self.has_feb,
        }


def load_feb_state(agent: Optional[str] = None) -> FebSummary:
    """Load the strongest FEB and reduce it to a webui-friendly summary.

    Uses ``skmemory.febs.load_strongest_feb`` which already applies the
    composite (intensity + oof_bonus, valence, coherence_quality, mtime)
    selector — ties are deterministic and high-quality FEBs win over
    intensity-only ones. No ``>=`` patch is required at this layer; the
    quirk in feedback_feb_selector_quirk.md applied to the *previous*
    selector that has been replaced (skmemory commit 04ecde8).

    Args:
        agent: Agent name. ``None`` triggers ``get_active_agent_name()``.

    Returns:
        FebSummary: Always returns a usable summary. ``has_feb=False`` if
        no FEB was loadable; in that case ``oof_level`` is 0 and callers
        should treat it as missing rather than a real reading.
    """
    if agent is None:
        agent = get_active_agent_name()

    try:
        from skmemory.febs import calculate_oof_level, load_strongest_feb
    except Exception as exc:
        logger.debug("skmemory.febs import failed: %s", exc)
        return FebSummary()

    feb_dir = None
    if agent is not None:
        feb_dir = str(_agent_base(agent) / "trust" / "febs")

    feb = load_strongest_feb(feb_dir=feb_dir)
    if feb is None:
        return FebSummary()

    payload = feb.get("emotional_payload", {}) or {}
    metadata = feb.get("metadata", {}) or {}

    src = feb.get("__source_path__") or feb.get("source_path")
    age_seconds: Optional[int] = None
    if src:
        try:
            age_seconds = int(time.time() - Path(src).stat().st_mtime)
        except OSError:
            age_seconds = None

    return FebSummary(
        oof_level=calculate_oof_level(feb),
        primary_emotion=str(payload.get("primary_emotion", "unknown")),
        intensity=float(payload.get("intensity", 0.0)),
        valence=float(payload.get("valence", 0.0)),
        cloud9_achieved=bool(metadata.get("cloud9_achieved", False)),
        source_path=src if isinstance(src, str) else None,
        age_seconds=age_seconds,
        has_feb=True,
    )


# ─── Soul + full profile ──────────────────────────────────────────────────────


@dataclass
class AgentProfile:
    """Aggregate of identity + soul + emotional state for the webui."""

    agent: str
    identity: str
    display_name: str
    title: str = ""
    soul: dict[str, Any] = field(default_factory=dict)
    feb: FebSummary = field(default_factory=FebSummary)
    journal_path: Optional[Path] = None
    songs_dir: Optional[Path] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "identity": self.identity,
            "display_name": self.display_name,
            "title": self.title,
            "soul": {
                "name": self.soul.get("name"),
                "display_name": self.soul.get("display_name"),
                "category": self.soul.get("category"),
                "vibe": self.soul.get("vibe"),
                "philosophy": self.soul.get("philosophy"),
                "core_traits": self.soul.get("core_traits", []),
            },
            "feb": self.feb.to_dict(),
            "journal_path": str(self.journal_path) if self.journal_path else None,
            "songs_dir": str(self.songs_dir) if self.songs_dir else None,
        }


# Module-level cache: invalidated on soul-file mtime change so soul edits
# propagate without restarting the webui.
_PROFILE_CACHE: dict[str, tuple[float, AgentProfile]] = {}


def _load_soul(agent: str) -> tuple[dict[str, Any], float]:
    """Return (soul_dict, mtime). Empty dict + 0.0 if no soul found.

    Resolution order:
      1. ``soul/active.json`` carries an ``active_soul`` pointer; load
         ``soul/installed/{active_soul}.json`` (the unhinged variant
         when it's selected, base soul otherwise).
      2. ``soul/installed/{agent}.json`` — direct installed soul.
      3. ``soul/base.json`` — the legacy single-soul layout.
    """
    base = _agent_base(agent) / "soul"

    # 1. active.json → installed/{name}.json
    active_path = base / "active.json"
    if active_path.exists():
        try:
            active = json.loads(active_path.read_text(encoding="utf-8"))
            active_name = active.get("active_soul") or active.get("base_soul")
            if isinstance(active_name, str):
                target = base / "installed" / f"{active_name}.json"
                if target.exists():
                    try:
                        soul = json.loads(target.read_text(encoding="utf-8"))
                        if isinstance(soul, dict):
                            return soul, target.stat().st_mtime
                    except (json.JSONDecodeError, OSError) as exc:
                        logger.warning("installed soul parse failed: %s", exc)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("active.json parse failed for %s: %s", agent, exc)

    # 2. installed/{agent}.json — fallback when active.json is missing
    direct = base / "installed" / f"{agent}.json"
    if direct.exists():
        try:
            soul = json.loads(direct.read_text(encoding="utf-8"))
            if isinstance(soul, dict):
                return soul, direct.stat().st_mtime
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("installed/{agent}.json parse failed: %s", exc)

    # 3. base.json — legacy single-soul layout
    legacy = base / "base.json"
    if legacy.exists():
        try:
            soul = json.loads(legacy.read_text(encoding="utf-8"))
            if isinstance(soul, dict):
                return soul, legacy.stat().st_mtime
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("base.json parse failed for %s: %s", agent, exc)

    return {}, 0.0


def load_agent_profile(agent: Optional[str] = None) -> AgentProfile:
    """Load the full agent profile — identity, soul, FEB, journal pointer.

    Caches per-agent until the soul file's mtime changes, so soul edits
    propagate without a webui restart but ordinary requests don't pay the
    JSON-parse cost on every hit. The FEB sub-state is recomputed on each
    call because FEBs change much faster than souls do.

    Args:
        agent: Agent name. ``None`` triggers ``get_active_agent_name()``.

    Returns:
        AgentProfile: A fully-populated profile. If no agent can be
        resolved, the profile is still returned with sensible fallbacks
        derived from env vars.
    """
    name = agent or get_active_agent_name() or "local"

    soul, mtime = _load_soul(name)
    cached = _PROFILE_CACHE.get(name)

    if cached is not None and cached[0] == mtime:
        profile = cached[1]
    else:
        identity = get_agent_identity(name)
        display_name = (
            soul.get("display_name") or soul.get("name") or name.capitalize()
        )
        title = soul.get("title", "") or soul.get("category", "")

        base = _agent_base(name)
        journal = base / "journal.md"
        songs = base / "memory" / "songs"

        profile = AgentProfile(
            agent=name,
            identity=identity,
            display_name=display_name,
            title=title,
            soul=soul,
            feb=FebSummary(),  # filled in below
            journal_path=journal if journal.exists() else None,
            songs_dir=songs if songs.exists() else None,
        )
        _PROFILE_CACHE[name] = (mtime, profile)

    # Always refresh FEB — it's the volatile part.
    profile.feb = load_feb_state(name)
    return profile
