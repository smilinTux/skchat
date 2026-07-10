"""Tests for the Telegram bridge `/model` live model-swap command + per-chat
model routing (scripts/telegram_bridge.py).

Covers:
  - `/model` / `/model list` roster + current-model reply
  - `/model <role>` writes the skmodels registry `contexts:` toggle
    (chat:<id> -> role) and flips resolution to the new backend
  - unknown roles are rejected without mutating the registry
  - registry comments are preserved on write (single-source-of-truth self-doc)
  - the backend call attaches `x-sk-context: chat:<id>` and uses the per-chat
    resolved url + model

The bridge module is heavy to import (spawns SystemPromptBuilder, wires
bridge_consciousness). We set a dummy token + a throwaway registry via env
BEFORE import, and skip if skos.models / the bridge deps aren't importable.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
SKCOMMS_SRC = REPO.parent / "skcomms" / "src"

_SRC_REGISTRY = Path.home() / ".skcapstone" / "models" / "registry.yaml"


@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    """Import telegram_bridge with a dummy token + a temp registry copy."""
    if not _SRC_REGISTRY.exists():
        pytest.skip("skmodels registry.yaml not present")
    reg = tmp_path / "registry.yaml"
    reg.write_text(_SRC_REGISTRY.read_text())
    monkeypatch.setenv("SKMODELS_REGISTRY", str(reg))
    monkeypatch.setenv("TELEGRAM_OPUS_BOT_TOKEN", "dummy-token")
    monkeypatch.setenv("SKC_BRIDGE_LLM_URL", "http://example.invalid/v1/chat/completions")
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


def test_model_list_shows_roles_and_current(bridge):
    tb, _ = bridge
    out = tb._handle_model_command(CHAT, "/model")
    assert "Available models" in out
    assert "sk-vision" in out
    assert "current:" in out and "ornith" in out  # default role -> ornith


def test_model_atmention_form(bridge):
    tb, _ = bridge
    assert tb._handle_model_command(CHAT, "/model@seaBird_Opus_bot") is not None


def test_unknown_role_rejected_without_write(bridge):
    tb, _ = bridge
    before = dict(tb._skmodels.list_contexts())
    out = tb._handle_model_command(CHAT, "/model sk-nonsense")
    assert "unknown model" in out
    assert dict(tb._skmodels.list_contexts()) == before


def test_swap_writes_context_and_flips_resolution(bridge):
    tb, reg = bridge
    out = tb._handle_model_command(CHAT, "/model sk-vision")
    assert "switched" in out and "sk-vision" in out
    assert tb._skmodels.list_contexts().get(f"chat:{CHAT}") == "sk-vision"
    b = tb._skmodels.resolve(context=f"chat:{CHAT}")
    assert b.name == "qwen-vl" and b.vision is True
    # comments preserved (self-documenting single source of truth)
    raw = reg.read_text()
    assert "SINGLE SOURCE OF TRUTH" in raw


def test_resolve_backend_for_chat_follows_swap(bridge):
    tb, _ = bridge
    tb._handle_model_command(CHAT, "/model sk-vision")
    url, model = tb._resolve_backend_for_chat(CHAT)
    assert url.endswith("/chat/completions") and "100.81.238.58" in url
    assert model == "Qwen3.6-27b-abliterated-Q4_K_M"


def test_call_attaches_context_header_and_resolved_backend(bridge, monkeypatch):
    tb, _ = bridge
    tb._handle_model_command(CHAT, "/model sk-vision")
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


def test_unset_reverts_to_default(bridge):
    tb, _ = bridge
    tb._handle_model_command(CHAT, "/model sk-vision")
    tb._skmodels.unset_context(f"chat:{CHAT}")
    assert tb._skmodels.resolve(context=f"chat:{CHAT}").name == "ornith"


def test_external_registry_edit_picked_up_live(bridge):
    """A toggle written by ANOTHER process (CLI `skmodels set` / Syncthing) must
    take effect in the long-running bridge without a restart — _resolve_backend_
    for_chat drops the path-keyed cache before resolving."""
    tb, reg = bridge
    # baseline: no per-chat context yet, so the default route applies
    # (sk-auto via the gateway these days; do not pin a backend host here,
    # the point of this test is the LIVE pickup of an external edit).
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
