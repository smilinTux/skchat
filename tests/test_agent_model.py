"""Tests for skchat.agent_model — per-agent chat-model selection."""

import importlib

import pytest


@pytest.fixture()
def am(tmp_path, monkeypatch):
    """agent_model module pointed at a temp state file, default model reset."""
    monkeypatch.setenv("SKCHAT_AGENT_MODEL_PATH", str(tmp_path / "agent_model.json"))
    monkeypatch.delenv("SKCHAT_LLM_MODEL", raising=False)
    import skchat.agent_model as module

    return importlib.reload(module)


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
