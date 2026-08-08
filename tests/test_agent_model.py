"""Tests for skchat.agent_model — per-agent chat-model selection."""

import importlib
import json

import pytest


@pytest.fixture()
def am(tmp_path, monkeypatch):
    """agent_model module pointed at temp state, default model reset.

    Both stores are isolated to *tmp_path*: the legacy JSON file
    (``SKCHAT_AGENT_MODEL_PATH``) and the skmodels registry
    (``SKMODELS_REGISTRY``) that is now the source of truth. The SKGateway
    free-model fetch is stubbed to ``[]`` by default so tests are deterministic
    and offline (no dependency on a live gateway). Tests that exercise the merge
    re-``setattr`` the fetcher to supply fake free models.
    """
    monkeypatch.setenv("SKCHAT_AGENT_MODEL_PATH", str(tmp_path / "agent_model.json"))
    monkeypatch.setenv("SKMODELS_REGISTRY", str(tmp_path / "registry.yaml"))
    monkeypatch.delenv("SKCHAT_LLM_MODEL", raising=False)
    import skchat.agent_model as module

    module = importlib.reload(module)
    monkeypatch.setattr(module, "_fetch_gateway_free_models", lambda: [])
    module._gateway_cache["at"] = 0.0
    module._gateway_cache["models"] = []
    return module


def test_default_when_unset(am):
    assert am.get_model("lumina") == "claude-opus-4-8"


def test_default_honours_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_AGENT_MODEL_PATH", str(tmp_path / "m.json"))
    monkeypatch.setenv("SKMODELS_REGISTRY", str(tmp_path / "registry.yaml"))
    monkeypatch.setenv("SKCHAT_LLM_MODEL", "qwen3.6-27b-abliterated")
    import skchat.agent_model as module

    module = importlib.reload(module)
    assert module.get_model("lumina") == "qwen3.6-27b-abliterated"


def test_set_then_get_roundtrip(am):
    am.set_model("lumina", "qwen3.6-27b-abliterated")
    assert am.get_model("lumina") == "qwen3.6-27b-abliterated"


def test_set_is_per_agent(am):
    am.set_model("lumina", "qwen3.6-27b-abliterated")
    am.set_model("opus", "claude-sonnet-4-6")
    assert am.get_model("lumina") == "qwen3.6-27b-abliterated"
    assert am.get_model("opus") == "claude-sonnet-4-6"


def test_set_rejects_unknown_model(am):
    with pytest.raises(ValueError):
        am.set_model("lumina", "gpt-4o")


def test_get_falls_back_when_stored_value_invalid(am):
    # A stale/invalid selection in the registry context (e.g. a model that was
    # removed from the picker AND is not a registry role/backend) is treated as
    # unset -> we fall back to the default rather than route somewhere dead.
    am.set_model("lumina", "claude-sonnet-4-6")  # writes the registry context
    import skos.models as skm

    skm.set_context("agent:lumina", "no-longer-supported")  # corrupt it
    assert am.get_model("lumina") == am.default_model()


def test_list_models_includes_required(am):
    ids = {m["id"] for m in am.list_models()}
    assert {"claude-opus-4-8", "qwen3.6-27b-abliterated"} <= ids


# --- SKGateway free-model merge ---------------------------------------------


def _fake_free(*ids_providers):
    """Build a picker-shaped free-model list for monkeypatching the fetcher."""
    return [
        {"id": mid, "label": mid, "provider": prov, "local": False} for mid, prov in ids_providers
    ]


def test_list_models_merges_gateway_free_after_curated(am, monkeypatch):
    monkeypatch.setattr(
        am,
        "_fetch_gateway_free_models",
        lambda: _fake_free(("openai/gpt-oss-20b", "nvidia"), ("x-ai/grok-free", "openrouter")),
    )
    models = am.list_models()
    ids = [m["id"] for m in models]
    # Curated 5 come first, in order, then the gateway free models.
    assert ids[:5] == [m["id"] for m in am.AVAILABLE_MODELS]
    assert "openai/gpt-oss-20b" in ids
    assert "x-ai/grok-free" in ids
    # Provider is carried through from the gateway entry.
    by_id = {m["id"]: m for m in models}
    assert by_id["openai/gpt-oss-20b"]["provider"] == "nvidia"
    assert by_id["x-ai/grok-free"]["provider"] == "openrouter"
    assert by_id["openai/gpt-oss-20b"]["local"] is False


def test_list_models_dedupes_curated_wins(am, monkeypatch):
    # Gateway also advertises a curated id — the curated entry must win.
    monkeypatch.setattr(
        am,
        "_fetch_gateway_free_models",
        lambda: _fake_free(("claude-opus-4-8", "nvidia"), ("openai/gpt-oss-20b", "nvidia")),
    )
    models = am.list_models()
    opus = [m for m in models if m["id"] == "claude-opus-4-8"]
    assert len(opus) == 1
    assert opus[0]["provider"] == "anthropic"  # curated, not the gateway's "nvidia"


def test_list_models_falls_back_to_curated_when_gateway_down(am, monkeypatch):
    # Gateway unreachable -> fetch yields nothing -> exactly the curated 5.
    monkeypatch.setattr(am, "_fetch_gateway_free_models", lambda: [])
    am._gateway_cache["at"] = 0.0
    am._gateway_cache["models"] = []
    models = am.list_models()
    assert [m["id"] for m in models] == [m["id"] for m in am.AVAILABLE_MODELS]
    assert len(models) == 5


def test_set_model_accepts_gateway_free_id(am, monkeypatch):
    monkeypatch.setattr(
        am, "_fetch_gateway_free_models", lambda: _fake_free(("openai/gpt-oss-20b", "nvidia"))
    )
    am.set_model("lumina", "openai/gpt-oss-20b")
    # Persisted and honoured by get_model (routing reads this).
    assert am.get_model("lumina") == "openai/gpt-oss-20b"


def test_set_model_still_rejects_truly_unknown(am, monkeypatch):
    monkeypatch.setattr(
        am, "_fetch_gateway_free_models", lambda: _fake_free(("openai/gpt-oss-20b", "nvidia"))
    )
    with pytest.raises(ValueError):
        am.set_model("lumina", "totally/made-up-model")


def test_get_model_falls_back_when_gateway_id_no_longer_valid(am, monkeypatch):
    # Selected a free model while the gateway offered it...
    monkeypatch.setattr(
        am, "_fetch_gateway_free_models", lambda: _fake_free(("openai/gpt-oss-20b", "nvidia"))
    )
    am.set_model("lumina", "openai/gpt-oss-20b")
    # ...then the gateway stops offering it (and its cache is gone).
    monkeypatch.setattr(am, "_fetch_gateway_free_models", lambda: [])
    am._gateway_cache["at"] = 0.0
    am._gateway_cache["models"] = []
    assert am.get_model("lumina") == am.default_model()


def test_gateway_result_is_cached(am, monkeypatch):
    calls = {"n": 0}

    def _counting():
        calls["n"] += 1
        return _fake_free(("openai/gpt-oss-20b", "nvidia"))

    monkeypatch.setattr(am, "_fetch_gateway_free_models", _counting)
    am._gateway_cache["at"] = 0.0
    am._gateway_cache["models"] = []
    am.list_models()
    am.list_models()
    am.list_models()
    # Within the TTL the underlying fetch runs at most once.
    assert calls["n"] == 1


def test_fetch_filters_free_and_maps_shape(am, monkeypatch):
    payload = {
        "object": "list",
        "data": [
            # free -> included, provider carried through
            {"id": "openai/gpt-oss-20b", "provider": "nvidia", "free": True},
            # not free -> excluded
            {"id": "qwen/qwen3.5-122b-a10b", "owned_by": "nvidia", "advertised": True},
            # free but missing provider -> falls back to owned_by
            {"id": "some/other-free", "owned_by": "openrouter", "free": True},
            # free with no id -> skipped
            {"provider": "nvidia", "free": True},
        ],
    }

    class _Resp:
        status = 200

        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    # Restore the real fetcher (the `am` fixture stubs it to []) to exercise the
    # actual parse/filter/map path against the faked HTTP response.
    real_fetch = importlib.reload(am)._fetch_gateway_free_models
    out = real_fetch()
    ids = {m["id"] for m in out}
    assert ids == {"openai/gpt-oss-20b", "some/other-free"}
    by_id = {m["id"]: m for m in out}
    assert by_id["some/other-free"]["provider"] == "openrouter"
    assert all(m["local"] is False for m in out)


def test_fetch_returns_empty_on_network_error(am, monkeypatch):
    def _boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    real_fetch = importlib.reload(am)._fetch_gateway_free_models
    assert real_fetch() == []


# --- Model enablement (advertise allowlist) ---------------------------------


def test_list_managed_models_flags_enabled_from_gateway(am, monkeypatch):
    catalog = [
        {"id": "ornith-big", "provider": "chiap08-ornith", "advertised": True},
        {"id": "claude-opus-4-8", "provider": "anthropic", "advertised": True},
        {"id": "some/free-model", "provider": "openrouter", "advertised": False},
    ]
    monkeypatch.setattr(am, "_admin_get_models", lambda: catalog)
    out = am.list_managed_models()
    assert out["source"] == "gateway"
    assert out["models"] == catalog
    assert set(out["enabled"]) == {"ornith-big", "claude-opus-4-8"}


def test_list_managed_models_degrades_to_curated_when_gateway_down(am, monkeypatch):
    def _boom():
        raise OSError("connection refused")

    monkeypatch.setattr(am, "_admin_get_models", _boom)
    out = am.list_managed_models()
    assert out["source"] == "curated"
    # every curated model is present and flagged enabled
    ids = {m["id"] for m in out["models"]}
    assert "claude-opus-4-8" in ids and "ornith-tiny" in ids
    assert all(m["advertised"] is True for m in out["models"])
    assert set(out["enabled"]) == ids


def test_set_enabled_models_validates_shape(am):
    with pytest.raises(ValueError):
        am.set_enabled_models("not-a-list")
    with pytest.raises(ValueError):
        am.set_enabled_models([1, 2, 3])


def test_set_enabled_models_puts_to_gateway_and_returns_view(am, monkeypatch):
    put_calls = []
    monkeypatch.setattr(am, "_admin_put_advertise", lambda enabled: put_calls.append(enabled))
    # after the PUT, the manage view reflects the new advertised set
    monkeypatch.setattr(
        am,
        "_admin_get_models",
        lambda: [
            {"id": "ornith-big", "advertised": True},
            {"id": "claude-opus-4-8", "advertised": False},
        ],
    )
    out = am.set_enabled_models(["ornith-big"])
    assert put_calls == [["ornith-big"]]
    assert out["enabled"] == ["ornith-big"]


# --- CR-5.1: registry.yaml is the sole per-agent authority -------------------

import os  # noqa: E402
from pathlib import Path  # noqa: E402


def _seed_registry(yaml_text: str) -> None:
    """Write a registry fixture at $SKMODELS_REGISTRY and clear skos's cache."""
    import skos.models as skm

    Path(os.environ["SKMODELS_REGISTRY"]).write_text(yaml_text, encoding="utf-8")
    skm._invalidate()


# A registry with real backends + roles so role targets resolve to a backend.
_REG_WITH_ROLES = """\
backends:
  ornith:
    url: http://192.168.0.100:8082/v1
    model: ornith-1.0-9b
    kind: chat
  opus:
    url: http://192.168.0.41:18780/v1
    model: claude-opus-4-8
    kind: chat
roles:
  ornith-tiny: ornith
  sk-default: ornith
  sk-heavy: opus
defaults:
  role: sk-default
"""


def test_set_model_writes_registry_context_not_json(am):
    """set_model persists the selection as the registry `agent:<name>` context;
    the legacy JSON file is NOT written (registry is the source of truth)."""
    import skos.models as skm

    am.set_model("lumina", "qwen3.6-27b-abliterated")
    assert skm.list_contexts().get("agent:lumina") == "qwen3.6-27b-abliterated"
    # Legacy JSON stays absent (no divergent second copy).
    assert not am._state_path().exists()
    assert am.get_model("lumina") == "qwen3.6-27b-abliterated"


def test_registry_context_wins_over_legacy_json(am):
    """When both stores have a value, the registry context wins."""
    # Legacy JSON says opus; registry says qwen -> registry wins.
    am._state_path().parent.mkdir(parents=True, exist_ok=True)
    am._state_path().write_text('{"lumina": "claude-opus-4-8"}', encoding="utf-8")
    am.set_model("lumina", "qwen3.6-27b-abliterated")
    assert am.get_model("lumina") == "qwen3.6-27b-abliterated"


def test_legacy_json_used_when_registry_context_unset(am):
    """A pre-migration JSON selection is still honoured when the registry has no
    `agent:<name>` context (read-fallback), so nothing is lost before migration."""
    am._state_path().parent.mkdir(parents=True, exist_ok=True)
    am._state_path().write_text('{"opus": "claude-sonnet-4-6"}', encoding="utf-8")
    assert am.get_model("opus") == "claude-sonnet-4-6"


def test_set_model_accepts_a_registry_role(am):
    """An operator can pin a logical role (not only a concrete id) as the model;
    it round-trips and is stored as the context target verbatim."""
    _seed_registry(_REG_WITH_ROLES)
    am.set_model("lumina", "sk-default")
    import skos.models as skm

    assert skm.list_contexts().get("agent:lumina") == "sk-default"
    assert am.get_model("lumina") == "sk-default"


def test_migrate_legacy_agent_models_idempotent(am):
    """Legacy JSON entries migrate into registry contexts, and re-running skips
    already-set entries (idempotent) and non-routable values."""
    _seed_registry(_REG_WITH_ROLES)
    am._state_path().parent.mkdir(parents=True, exist_ok=True)
    am._state_path().write_text(
        '{"lumina": "ornith-tiny", "opus": "sk-heavy", "ghost": "made-up-model"}',
        encoding="utf-8",
    )

    first = am.migrate_legacy_agent_models()
    migrated = dict(first["migrated"])
    assert migrated == {"lumina": "ornith-tiny", "opus": "sk-heavy"}
    # non-routable value is reported as skipped, never written
    assert any(a == "ghost" for a, _ in first["skipped"])

    import skos.models as skm

    ctx = skm.list_contexts()
    assert ctx.get("agent:lumina") == "ornith-tiny"
    assert ctx.get("agent:opus") == "sk-heavy"

    # Re-run: everything already set -> nothing migrated again.
    second = am.migrate_legacy_agent_models()
    assert second["migrated"] == []
    assert {a for a, _ in second["skipped"]} >= {"lumina", "opus"}


def test_e2e_registry_is_the_shared_resolver(am):
    """End-to-end (AC-verify): a model set via skchat lands in the registry
    context, get_model reads it back, and skos.models.resolve() (the SAME
    resolver the gateway uses) resolves that context to the concrete backend."""
    _seed_registry(_REG_WITH_ROLES)
    import skos.models as skm

    # 1. set via skchat -> writes the agent:<name> context
    am.set_model("lumina", "ornith-tiny")
    # 2. skchat get_model returns it
    assert am.get_model("lumina") == "ornith-tiny"
    # 3. the shared resolver resolves the SAME context to the concrete backend
    b = skm.resolve(context="agent:lumina")
    assert b.name == "ornith"
    assert b.model == "ornith-1.0-9b"
    # sanity: a heavy role resolves to the opus backend too
    am.set_model("opus", "sk-heavy")
    assert skm.resolve(context="agent:opus").model == "claude-opus-4-8"
