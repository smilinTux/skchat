"""Tests for the Telegram bridge `/model` command + reply-backend resolution
(scripts/telegram_bridge.py), unified onto the shared per-agent selection
store + resolver (skchat.agent_model / skchat.reply_model).

Covers:
  - `/model` / `/model list` shows roles AND models (skchat.agent_model's
    dynamic catalog), current selection marked
  - `/model <role-or-model-id>` sets the shared PER-AGENT selection
    (skchat.agent_model.set_selection) , NOT a chat-scoped write
  - `/model pin <role>` still writes the pre-existing skos.models registry
    `contexts:` toggle (chat:<id> -> role), a Telegram-only override
  - unknown roles/models are rejected without mutating either store
  - registry comments are preserved on a pin write (single-source-of-truth
    self-doc)
  - `_resolve_backend_for_chat` honors precedence: chat pin > per-agent
    selection > default, and attaches `x-sk-context: chat:<id>` on the call

The bridge module is heavy to import (spawns SystemPromptBuilder, wires
bridge_consciousness). We set a dummy token + a throwaway registry via env
BEFORE import, and skip if skos.models / the bridge deps aren't importable.

CRITICAL: the per-agent selection store lives at a REAL, shared path by
default (``~/.skchat/agent_model.json``), same file the live daemon/app/voice
read. Every test here redirects it to a tmp_path via
``SKCHAT_AGENT_MODEL_PATH`` so nothing here ever touches live agent state.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
SKCOMMS_SRC = REPO.parent / "skcomms" / "src"

_SRC_REGISTRY = Path.home() / ".skcapstone" / "models" / "registry.yaml"


@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    """Import telegram_bridge with a dummy token, a temp skos registry copy,
    and a temp per-agent selection store (never the real ~/.skchat one)."""
    if not _SRC_REGISTRY.exists():
        pytest.skip("skmodels registry.yaml not present")
    reg = tmp_path / "registry.yaml"
    reg.write_text(_SRC_REGISTRY.read_text())
    monkeypatch.setenv("SKMODELS_REGISTRY", str(reg))
    monkeypatch.setenv("TELEGRAM_OPUS_BOT_TOKEN", "dummy-token")
    monkeypatch.setenv("SKC_BRIDGE_LLM_URL", "http://example.invalid/v1/chat/completions")
    monkeypatch.setenv("SKCHAT_AGENT_MODEL_PATH", str(tmp_path / "agent_model.json"))
    monkeypatch.setenv("SKAGENT", "opus")
    for p in (str(SCRIPTS), str(SKCOMMS_SRC)):
        if p not in sys.path:
            sys.path.insert(0, p)
    sys.modules.pop("telegram_bridge", None)
    try:
        import telegram_bridge as tb  # noqa: WPS433
    except Exception as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"telegram_bridge import failed: {exc}")
    if tb._skmodels is None:  # pragma: no cover
        pytest.skip("skos.models unavailable in this env")
    # invalidate any cached registry so resolution reads our temp copy
    tb._skmodels.load_registry.cache_clear() if hasattr(
        tb._skmodels.load_registry, "cache_clear") else None
    return tb, reg


CHAT = 778899


def test_non_command_passes_through(bridge):
    tb, _ = bridge
    assert tb._handle_model_command(CHAT, "hello there") is None


def test_model_list_shows_roles_and_models_and_current(bridge):
    tb, _ = bridge
    out = tb._handle_model_command(CHAT, "/model")
    assert "Roles:" in out and "Models:" in out
    assert "sk-vision" in out
    # unset selection defaults to ornith-tiny, marked as current
    assert "ornith-tiny" in out and "<- current" in out


def test_model_list_alias(bridge):
    tb, _ = bridge
    assert tb._handle_model_command(CHAT, "/model list") == tb._handle_model_command(
        CHAT, "/model"
    )


def test_model_atmention_form(bridge):
    tb, _ = bridge
    assert tb._handle_model_command(CHAT, "/model@seaBird_Opus_bot") is not None


def test_unknown_selection_rejected_without_write(bridge):
    tb, _ = bridge
    from skchat.agent_model import get_selection

    before = get_selection("opus")
    out = tb._handle_model_command(CHAT, "/model sk-nonsense")
    assert "unknown model" in out
    assert get_selection("opus") == before


def test_swap_sets_shared_agent_selection_not_chat_pin(bridge):
    tb, _ = bridge
    from skchat.agent_model import get_selection

    out = tb._handle_model_command(CHAT, "/model sk-vision")
    assert "switched" in out and "sk-vision" in out
    assert get_selection("opus") == "sk-vision"
    # a plain /model <role> must NOT write the chat-scoped skos pin
    assert tb._skmodels.list_contexts().get(f"chat:{CHAT}") is None


def test_swap_marks_current_in_list(bridge):
    tb, _ = bridge
    tb._handle_model_command(CHAT, "/model sk-vision")
    out = tb._handle_model_command(CHAT, "/model list")
    assert "sk-vision  <- current" in out


def test_pin_writes_chat_context_and_preserves_comments(bridge):
    tb, reg = bridge
    out = tb._handle_model_command(CHAT, "/model pin sk-vision")
    assert "pinned" in out and "sk-vision" in out
    assert tb._skmodels.list_contexts().get(f"chat:{CHAT}") == "sk-vision"
    b = tb._skmodels.resolve(context=f"chat:{CHAT}")
    assert b.name == "qwen-vl" and b.vision is True
    # comments preserved (self-documenting single source of truth)
    raw = reg.read_text()
    assert "SINGLE SOURCE OF TRUTH" in raw


def test_pin_unknown_role_rejected_without_write(bridge):
    tb, _ = bridge
    before = dict(tb._skmodels.list_contexts())
    out = tb._handle_model_command(CHAT, "/model pin sk-nonsense")
    assert "unknown model" in out
    assert dict(tb._skmodels.list_contexts()) == before


def test_pin_without_role_shows_usage(bridge):
    tb, _ = bridge
    out = tb._handle_model_command(CHAT, "/model pin")
    assert "usage" in out


def test_resolve_backend_for_chat_follows_agent_selection(bridge):
    tb, _ = bridge
    tb._handle_model_command(CHAT, "/model sk-vision")
    url, model = tb._resolve_backend_for_chat(CHAT)
    assert url.endswith("/chat/completions") and "100.81.238.58" in url
    assert model == "Qwen3.6-27b-abliterated-Q4_K_M"


def test_chat_pin_wins_over_agent_selection(bridge):
    tb, _ = bridge
    # agent selection says sk-code, but THIS chat is pinned to sk-vision
    tb._handle_model_command(CHAT, "/model sk-code")
    tb._handle_model_command(CHAT, "/model pin sk-vision")
    url, model = tb._resolve_backend_for_chat(CHAT)
    assert "100.81.238.58" in url  # the pinned VL backend, not sk-code's
    assert model == "Qwen3.6-27b-abliterated-Q4_K_M"
    # a DIFFERENT chat with no pin still follows the agent-wide selection
    other_url, other_model = tb._resolve_backend_for_chat(CHAT + 1)
    assert (other_url, other_model) != (url, model)


def test_call_attaches_context_header_and_resolved_backend(bridge, monkeypatch):
    tb, _ = bridge
    tb._handle_model_command(CHAT, "/model pin sk-vision")
    url, model = tb._resolve_backend_for_chat(CHAT)

    captured: dict = {}

    class _Resp:
        def read(self):
            return json.dumps(
                {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}
            ).encode()

    def _fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        captured["body"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr(tb.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(tb, "_TOOLS_CACHE", [])
    # _run_tool_loop returns (reply, concrete_model); the fake response
    # carries no "model" key so the concrete model is None here.
    out, concrete = tb._run_tool_loop([{"role": "user", "content": "hi"}],
                                      chat_id=str(CHAT), url=url, model=model)
    assert out == "hi"
    assert concrete is None
    assert captured["headers"].get("x-sk-context") == f"chat:{CHAT}"
    assert captured["url"] == url
    assert captured["body"]["model"] == model


def test_unpin_reverts_to_agent_selection(bridge):
    tb, _ = bridge
    tb._handle_model_command(CHAT, "/model sk-code")
    tb._handle_model_command(CHAT, "/model pin sk-vision")
    tb._skmodels.unset_context(f"chat:{CHAT}")
    url, model = tb._resolve_backend_for_chat(CHAT)
    assert "100.81.238.58" not in url  # no longer the pinned VL backend


def test_external_registry_pin_edit_picked_up_live(bridge):
    """A pin written by ANOTHER process (CLI `skmodels set` / Syncthing) must
    take effect in the long-running bridge without a restart , _chat_pin_
    resolve drops the path-keyed cache before resolving."""
    tb, reg = bridge
    # baseline: no per-chat pin yet, so the (default) agent selection applies
    url0, model0 = tb._resolve_backend_for_chat(CHAT)
    assert "100.81.238.58" not in url0
    # simulate an external edit to the synced registry (no in-process
    # set_context): rewrite the file through YAML so this stays robust no
    # matter what contexts the source registry snapshot already carries.
    import yaml

    data = yaml.safe_load(reg.read_text())
    contexts = data.get("contexts") or {}
    contexts[f"chat:{CHAT}"] = "sk-vision"
    data["contexts"] = contexts
    reg.write_text(yaml.safe_dump(data))
    url1, model1 = tb._resolve_backend_for_chat(CHAT)
    assert "100.81.238.58" in url1  # now the VL backend, live
    assert model1 == "Qwen3.6-27b-abliterated-Q4_K_M"
    assert (url1, model1) != (url0, model0)  # the external edit took effect
