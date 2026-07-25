import importlib

import skchat.agent_model as AM


def test_default_selection_is_ornith_tiny():
    assert AM.default_selection() == "ornith-tiny"


def test_set_and_get_selection_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_AGENT_MODEL_PATH", str(tmp_path / "sel.json"))
    importlib.reload(AM)
    AM.set_selection("lumina", "claude-opus-4-8",
                     valid_roles={"sk-creative"}, valid_models={"claude-opus-4-8", "ornith-tiny"})
    assert AM.get_selection("lumina") == "claude-opus-4-8"


def test_set_selection_accepts_role(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_AGENT_MODEL_PATH", str(tmp_path / "sel.json"))
    importlib.reload(AM)
    AM.set_selection("lumina", "sk-creative", valid_roles={"sk-creative"}, valid_models=set())
    assert AM.get_selection("lumina") == "sk-creative"
    assert AM.classify("sk-creative", {"sk-creative"}) == "role"
    assert AM.classify("ornith-tiny", {"sk-creative"}) == "model"


def test_set_selection_rejects_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_AGENT_MODEL_PATH", str(tmp_path / "sel.json"))
    importlib.reload(AM)
    import pytest
    with pytest.raises(ValueError):
        AM.set_selection("lumina", "bogus-model", valid_roles={"sk-creative"}, valid_models={"ornith-tiny"})


def test_get_selection_defaults_when_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_AGENT_MODEL_PATH", str(tmp_path / "sel.json"))
    importlib.reload(AM)
    assert AM.get_selection("newagent") == "ornith-tiny"
