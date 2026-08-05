"""Tests for skchat.agent_model — per-agent chat-model selection."""

import importlib
import json

import pytest


@pytest.fixture()
def am(tmp_path, monkeypatch):
    """agent_model module pointed at a temp state file, default model reset.

    The SKGateway free-model fetch is stubbed to ``[]`` by default so tests are
    deterministic and offline (no dependency on a live gateway).  Tests that
    exercise the merge re-``setattr`` the fetcher to supply fake free models.
    """
    monkeypatch.setenv("SKCHAT_AGENT_MODEL_PATH", str(tmp_path / "agent_model.json"))
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
    # Simulate a stale/invalid stored selection (e.g. model removed from list).
    am.set_model("lumina", "claude-sonnet-4-6")
    path = am._state_path()
    path.write_text('{"lumina": "no-longer-supported"}', encoding="utf-8")
    assert am.get_model("lumina") == am.default_model()


def test_list_models_includes_required(am):
    ids = {m["id"] for m in am.list_models()}
    assert {"claude-opus-4-8", "qwen3.6-27b-abliterated"} <= ids


# --- SKGateway free-model merge ---------------------------------------------


def _fake_free(*ids_providers):
    """Build a picker-shaped free-model list for monkeypatching the fetcher."""
    return [
        {"id": mid, "label": mid, "provider": prov, "local": False}
        for mid, prov in ids_providers
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
