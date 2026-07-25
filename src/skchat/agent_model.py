"""Per-agent chat-model selection.

Shared state between the in-app model picker (daemon HTTP API on :9385) and the
consciousness bridge (``scripts/lumina-bridge.py``).  The picker writes the
selected model here; the bridge reads it for the next reply and routes the
request through SKGateway (``SKCHAT_LLM_URL``).

State lives in a tiny JSON file so the two separate processes (daemon + bridge)
agree without a database:

    ~/.skchat/agent_model.json   ->  {"lumina": "claude-opus-4-8", ...}

Override the path with ``SKCHAT_AGENT_MODEL_PATH``.  The default model (used
when no selection has been made) comes from ``SKCHAT_LLM_MODEL`` or falls back
to ``claude-opus-4-8``.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

# Curated, user-selectable models.  Each MUST be routable by the configured
# SKGateway (``/v1/chat/completions``).  Order = display order in the picker.
AVAILABLE_MODELS: list[dict] = [
    {"id": "claude-opus-4-8", "label": "Claude Opus 4.8", "provider": "anthropic", "local": False},
    {
        "id": "claude-sonnet-4-6",
        "label": "Claude Sonnet 4.6",
        "provider": "anthropic",
        "local": False,
    },
    {
        "id": "claude-haiku-4-5",
        "label": "Claude Haiku 4.5",
        "provider": "anthropic",
        "local": False,
    },
    {
        "id": "ornith-tiny",
        "label": "Ornith Tiny 9B NVFP4+MTP (local)",
        "provider": "local",
        "local": True,
    },
]

_VALID_IDS = {m["id"] for m in AVAILABLE_MODELS}

_LEGACY_ALIASES = {"qwen3.6-27b-abliterated": "ornith-tiny"}

# Shared knob for capable/reasoning jobs (NOT for uncensored jobs, those must stay
# on a local uncensored model). Chef will flip this to "ornith-big" once that
# backend is live; until then it defaults to Claude Opus.
CAPABLE_MODEL = os.environ.get("SKCHAT_CAPABLE_MODEL", "claude-opus-4-8")

_lock = threading.Lock()


def _state_path() -> Path:
    return Path(
        os.environ.get("SKCHAT_AGENT_MODEL_PATH", "~/.skchat/agent_model.json")
    ).expanduser()


def default_model() -> str:
    """The model used when an agent has no explicit selection."""
    return os.environ.get("SKCHAT_LLM_MODEL", "claude-opus-4-8")


def default_selection() -> str:
    """The fast local default (ornith-tiny on .100). Was claude-opus; changed for
    speed."""
    return "ornith-tiny"


def list_models() -> list[dict]:
    """Return the curated list of selectable models (copy)."""
    return [dict(m) for m in AVAILABLE_MODELS]


def list_choices(*, gateway_fetch, roles_source) -> dict:
    """Dynamic catalog: skos roles + SKGateway-served models (legacy aliases
    collapsed to their canonical id so one backend shows once)."""
    roles = list(roles_source() or [])
    seen = set()
    models = []
    for m in (gateway_fetch() or {}).get("data", []):
        mid = _LEGACY_ALIASES.get(m.get("id"), m.get("id"))
        if not mid or mid in seen:
            continue
        seen.add(mid)
        models.append({"id": mid, "provider": m.get("provider"),
                       "free": m.get("free")})
    return {"roles": roles, "models": models}


def _read() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _load() -> dict:
    """Load agent selections from SKCHAT_AGENT_MODEL_PATH."""
    return _read()


def _save(data: dict) -> None:
    """Save agent selections to SKCHAT_AGENT_MODEL_PATH."""
    with _lock:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)


def classify(value: str, valid_roles: set) -> str:
    """Classify a value as 'role' or 'model' based on valid_roles set."""
    return "role" if value in valid_roles else "model"


def get_selection(agent: str) -> str:
    """Get the stored selection for agent, or the default if unset."""
    data = _load()
    return data.get(agent) or default_selection()


def set_selection(agent: str, value: str, *, valid_roles: set, valid_models: set
                  ) -> str:
    """Store a selection (role or model) for agent.

    Raises:
        ValueError: if value is neither a known role nor a served model.
    """
    if value not in valid_roles and value not in valid_models:
        raise ValueError(f"unknown selection {value!r} (not a role or served model)")
    data = _load()
    data[agent] = value
    _save(data)
    return value


def get_model(agent: str) -> str:
    """Return the selected model for *agent*, or the default if unset/invalid."""
    return get_selection(agent)


def set_model(agent: str, model: str) -> str:
    """Persist *model* as *agent*'s selection.

    Raises:
        ValueError: if *model* is not one of AVAILABLE_MODELS.
    """
    if model not in _VALID_IDS:
        raise ValueError(f"unknown model {model!r}; valid: {sorted(_VALID_IDS)}")
    return set_selection(agent, model, valid_roles=set(), valid_models=_VALID_IDS)
