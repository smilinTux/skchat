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

In addition to the curated ``AVAILABLE_MODELS`` above, ``list_models()`` merges
in the FREE models that SKGateway discovers and serves at
``$SKGATEWAY_URL/v1/models`` (default ``http://localhost:18780``).  This lets the
in-app picker surface the full free NIM/OpenRouter catalog without hard-coding
it here.  The gateway fetch is best-effort: it is wrapped in a short-timeout
try/except and cached for ~60s, so the picker (and the daemon GET/POST paths)
NEVER break when SKGateway is unreachable — they degrade to exactly the curated
models below.
"""

from __future__ import annotations

import json
import os
import threading
import time
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
    {
        "id": "qwen3.6-27b-abliterated",
        "label": "Qwen 3.6 27B (local)",
        "provider": "local",
        "local": True,
    },
]

_lock = threading.Lock()

# --- SKGateway free-model discovery (best-effort, cached) --------------------

# How long a successful gateway fetch stays cached before we re-query.
_GATEWAY_CACHE_TTL_S = 60.0
# Short timeout so a slow/hung gateway never stalls the picker or the daemon.
_GATEWAY_TIMEOUT_S = 2.5

_gateway_lock = threading.Lock()
_gateway_cache: dict = {"at": 0.0, "models": []}


def _gateway_base() -> str:
    return os.environ.get("SKGATEWAY_URL", "http://localhost:18780").rstrip("/")


def _label_for(model_id: str) -> str:
    """Human-ish label for a gateway model id (last path segment, spaced)."""
    tail = model_id.rsplit("/", 1)[-1]
    return tail.replace("-", " ").replace("_", " ").strip() or model_id


def _fetch_gateway_free_models() -> list[dict]:
    """Fetch SKGateway's FREE models, mapped to the picker shape.

    Returns ``[]`` on ANY failure (network, timeout, bad JSON, non-200).  The
    discovery marks free models with ``"free": true``; each is mapped to
    ``{"id","label","provider","local": False}`` with ``provider`` taken from the
    gateway entry (e.g. ``"nvidia"``/``"openrouter"``).
    """
    import urllib.request

    url = f"{_gateway_base()}/v1/models"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_GATEWAY_TIMEOUT_S) as resp:
            if getattr(resp, "status", 200) != 200:
                return []
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        # Unreachable / timeout / malformed — degrade to curated-only.
        return []

    out: list[dict] = []
    for entry in payload.get("data", []) or []:
        if not isinstance(entry, dict) or not entry.get("free"):
            continue
        mid = entry.get("id")
        if not mid:
            continue
        provider = entry.get("provider") or entry.get("owned_by") or "gateway"
        out.append(
            {
                "id": mid,
                "label": _label_for(mid),
                "provider": provider,
                "local": False,
            }
        )
    return out


def _gateway_free_models() -> list[dict]:
    """Cached wrapper around :func:`_fetch_gateway_free_models` (~60s TTL)."""
    now = time.monotonic()
    with _gateway_lock:
        if now - _gateway_cache["at"] < _GATEWAY_CACHE_TTL_S and _gateway_cache["models"]:
            return [dict(m) for m in _gateway_cache["models"]]
    models = _fetch_gateway_free_models()
    with _gateway_lock:
        # Only overwrite the cache timestamp/models on a real result; on failure
        # keep any previously-good list until its TTL lapses, but still refresh
        # the timestamp so we don't hammer a down gateway every call.
        _gateway_cache["at"] = now
        if models:
            _gateway_cache["models"] = models
        elif not _gateway_cache["models"]:
            _gateway_cache["models"] = []
        return [dict(m) for m in _gateway_cache["models"]]


def _state_path() -> Path:
    return Path(
        os.environ.get("SKCHAT_AGENT_MODEL_PATH", "~/.skchat/agent_model.json")
    ).expanduser()


def default_model() -> str:
    """The model used when an agent has no explicit selection."""
    return os.environ.get("SKCHAT_LLM_MODEL", "claude-opus-4-8")


def list_models() -> list[dict]:
    """Return the selectable models: curated first, then SKGateway free models.

    The 5 curated ``AVAILABLE_MODELS`` always come first and always win on id
    collisions.  SKGateway's discovered FREE models are appended after them,
    deduped by id.  If SKGateway is unreachable this returns exactly the curated
    list (best-effort, never raises).
    """
    merged = [dict(m) for m in AVAILABLE_MODELS]
    seen = {m["id"] for m in merged}
    for m in _gateway_free_models():
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        merged.append(dict(m))
    return merged


def _valid_ids() -> set[str]:
    """Set of ids selectable right now (curated + live gateway free models)."""
    return {m["id"] for m in list_models()}


# --- Model ENABLEMENT (advertise allowlist) ---------------------------------
# The gateway's advertise allowlist (/admin/models[,/advertise]) is the single
# source of truth for which discovered models are "enabled" (advertised on
# /v1/models, and therefore offered in the picker / to the brain). These helpers
# let the daemon (and thus the app + dashboard, same host) read and write it.

# Short timeout so a slow/hung gateway never stalls the manage UI or the daemon.
_ADMIN_TIMEOUT_S = 3.0


def _admin_get_models() -> list[dict]:
    """GET the gateway's FULL discovered catalog with ``advertised`` flags
    (loopback ``/admin/models``). Raises on any failure (caller degrades)."""
    import urllib.request

    url = f"{_gateway_base()}/admin/models"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_ADMIN_TIMEOUT_S) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload.get("data", []) or []


def _admin_put_advertise(enabled: list[str]) -> dict:
    """PUT the enabled/advertised allowlist to the gateway (loopback
    ``/admin/models/advertise``). Raises on any failure."""
    import urllib.request

    url = f"{_gateway_base()}/admin/models/advertise"
    body = json.dumps({"enabled": enabled}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="PUT", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=_ADMIN_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_managed_models() -> dict:
    """All discovered models + which are ENABLED (advertised), for the manage UI.

    Reads the gateway's ``/admin/models`` (the full catalog, each tagged
    ``advertised``). Returns ``{"models": [...], "enabled": [ids], "source": ...}``.
    An empty gateway allowlist means "advertise everything", so every model comes
    back ``advertised: True``. Degrades to the curated list (all enabled) when the
    gateway is unreachable; never raises.
    """
    try:
        data = _admin_get_models()
    except Exception:
        return {
            "models": [{**m, "advertised": True} for m in AVAILABLE_MODELS],
            "enabled": [m["id"] for m in AVAILABLE_MODELS],
            "source": "curated",
        }
    enabled = [m["id"] for m in data if m.get("advertised")]
    return {"models": data, "enabled": enabled, "source": "gateway"}


def set_enabled_models(enabled: list[str]) -> dict:
    """Persist the ENABLED/advertised model set to the gateway allowlist.

    ``enabled`` is the FULL set of model ids to advertise (an empty list means
    "advertise everything"). Validates the shape, PUTs to the gateway's loopback
    ``/admin/models/advertise``, then returns the refreshed manage view.

    Raises:
        ValueError: ``enabled`` is not a list of strings.
    """
    if not isinstance(enabled, list) or not all(isinstance(x, str) for x in enabled):
        raise ValueError("enabled must be a list of model-id strings")
    _admin_put_advertise(enabled)
    return list_managed_models()


def _read() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def get_model(agent: str) -> str:
    """Return the selected model for *agent*, or the default if unset/invalid.

    Validates against the merged list (curated + gateway free models), so a
    previously-selected free model keeps routing to itself.  The ~60s last-good
    gateway cache keeps a fetched free id valid for the process; if the id ever
    resolves as unknown (gateway never reachable this run) we fall back to the
    curated default rather than route to a model the gateway can't serve.
    """
    selected = _read().get(agent)
    if selected and selected in _valid_ids():
        return selected
    return default_model()


def set_model(agent: str, model: str) -> str:
    """Persist *model* as *agent*'s selection.

    Raises:
        ValueError: if *model* is not one of the selectable models (curated
            ``AVAILABLE_MODELS`` or a live SKGateway free model).
    """
    valid = _valid_ids()
    if model not in valid:
        raise ValueError(f"unknown model {model!r}; valid: {sorted(valid)}")
    with _lock:
        path = _state_path()
        data = _read()
        data[agent] = model
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    return model
