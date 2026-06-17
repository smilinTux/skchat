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
        "id": "qwen3.6-27b-abliterated",
        "label": "Qwen 3.6 27B (local)",
        "provider": "local",
        "local": True,
    },
]

_VALID_IDS = {m["id"] for m in AVAILABLE_MODELS}

_lock = threading.Lock()


def _state_path() -> Path:
    return Path(
        os.environ.get("SKCHAT_AGENT_MODEL_PATH", "~/.skchat/agent_model.json")
    ).expanduser()


def default_model() -> str:
    """The model used when an agent has no explicit selection."""
    return os.environ.get("SKCHAT_LLM_MODEL", "claude-opus-4-8")


def list_models() -> list[dict]:
    """Return the curated list of selectable models (copy)."""
    return [dict(m) for m in AVAILABLE_MODELS]


def _read() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def get_model(agent: str) -> str:
    """Return the selected model for *agent*, or the default if unset/invalid."""
    selected = _read().get(agent)
    if selected in _VALID_IDS:
        return selected
    return default_model()


def set_model(agent: str, model: str) -> str:
    """Persist *model* as *agent*'s selection.

    Raises:
        ValueError: if *model* is not one of AVAILABLE_MODELS.
    """
    if model not in _VALID_IDS:
        raise ValueError(f"unknown model {model!r}; valid: {sorted(_VALID_IDS)}")
    with _lock:
        path = _state_path()
        data = _read()
        data[agent] = model
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    return model
