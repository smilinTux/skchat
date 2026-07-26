"""Tests for the dormant `/code` autocode-dispatch command in the Telegram bridge.

`/code` runs a coding task through the autocode engine (skharness.autocode). It
ships DORMANT and SAFE: the whole handler is gated on SKC_BRIDGE_CODE_ENABLED
(default off) and an explicit allowlist (SKC_BRIDGE_CODE_ALLOWED_IDS, empty =
nobody), so merging it cannot affect the running bots. Default mode is `direct`
(DirectExecutor, one run, no grade, no merge); a leading `gated` keyword selects
the full loop (EngineeringExecutor). The telegram binding NEVER enables
automerge. See docs/superpowers/specs/2026-07-25-autocode-toggle-and-integration.md.

The engine run is always MOCKED here: no real coding task, no real subprocess.
telegram_bridge.py is loaded once via importlib with a dummy token and a tmp
agent home so no real agent state is touched.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture(scope="module")
def bridge(tmp_path_factory):
    """Load scripts/telegram_bridge.py once (heavy import) with a dummy token and
    an isolated agent home so the awakening reads no real agent state."""
    home = tmp_path_factory.mktemp("agent-home")
    saved = {
        k: os.environ.get(k)
        for k in (
            "SKC_BRIDGE_TOKEN",
            "SKC_BRIDGE_AGENT_HOME",
            "SKC_BRIDGE_CODE_ENABLED",
            "SKC_BRIDGE_CODE_ALLOWED_IDS",
            "SKC_BRIDGE_CODE_DEFAULT_REPO",
        )
    }
    os.environ["SKC_BRIDGE_TOKEN"] = "dummy:test-token"
    os.environ["SKC_BRIDGE_AGENT_HOME"] = str(home)
    for k in (
        "SKC_BRIDGE_CODE_ENABLED",
        "SKC_BRIDGE_CODE_ALLOWED_IDS",
        "SKC_BRIDGE_CODE_DEFAULT_REPO",
    ):
        os.environ.pop(k, None)
    import sys

    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "telegram_bridge_code_test", _SCRIPTS / "telegram_bridge.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture()
def mock_engine(bridge, monkeypatch):
    """Replace the engine seam with a Mock so the handler never touches the real
    engine. Returns the mock (default return value is a summary string)."""
    m = MagicMock(return_value="ran (mocked)")
    monkeypatch.setattr(bridge, "_run_autocode", m)
    return m


# ── parsing (pure) ──────────────────────────────────────────────────────────


class TestParse:
    def test_non_code_text_returns_none(self, bridge):
        assert bridge._parse_code_command("hello there") is None
        assert bridge._parse_code_command("/model list") is None

    def test_direct_is_the_default_mode(self, bridge, monkeypatch):
        monkeypatch.setenv("SKC_BRIDGE_CODE_DEFAULT_REPO", "")
        mode, repo, task = bridge._parse_code_command("/code repo=skchat fix a typo")
        assert mode == "direct"
        assert repo == "skchat"
        assert task == "fix a typo"

    def test_gated_keyword_selects_gated(self, bridge):
        mode, repo, task = bridge._parse_code_command("/code gated repo=skchat do it")
        assert mode == "gated"
        assert repo == "skchat"
        assert task == "do it"

    def test_at_mention_form_is_accepted(self, bridge):
        parsed = bridge._parse_code_command("/code@seaBird_Opus_bot repo=skchat x")
        assert parsed is not None
        assert parsed[1] == "skchat"

    def test_default_repo_used_when_not_named(self, bridge, monkeypatch):
        monkeypatch.setenv("SKC_BRIDGE_CODE_DEFAULT_REPO", "skskills")
        mode, repo, task = bridge._parse_code_command("/code tidy up")
        assert repo == "skskills"
        assert task == "tidy up"


# ── DORMANT: disabled by default ────────────────────────────────────────────


class TestDisabledByDefault:
    def test_flag_off_refuses_and_never_calls_engine(self, bridge, mock_engine, monkeypatch):
        monkeypatch.delenv("SKC_BRIDGE_CODE_ENABLED", raising=False)
        monkeypatch.setenv("SKC_BRIDGE_CODE_ALLOWED_IDS", "42")
        reply = bridge._handle_code_command(42, 42, "/code repo=skchat fix it")
        assert reply is not None
        assert "disabled" in reply.lower()
        mock_engine.assert_not_called()


# ── allowlist ───────────────────────────────────────────────────────────────


class TestAllowlist:
    def test_empty_allowlist_refuses_everyone(self, bridge, mock_engine, monkeypatch):
        monkeypatch.setenv("SKC_BRIDGE_CODE_ENABLED", "1")
        monkeypatch.setenv("SKC_BRIDGE_CODE_ALLOWED_IDS", "")
        reply = bridge._handle_code_command(42, 42, "/code repo=skchat fix it")
        assert reply is not None
        assert "allowlist" in reply.lower() or "refus" in reply.lower()
        mock_engine.assert_not_called()

    def test_non_allowlisted_id_refused(self, bridge, mock_engine, monkeypatch):
        monkeypatch.setenv("SKC_BRIDGE_CODE_ENABLED", "1")
        monkeypatch.setenv("SKC_BRIDGE_CODE_ALLOWED_IDS", "999")
        reply = bridge._handle_code_command(42, 42, "/code repo=skchat fix it")
        assert reply is not None
        mock_engine.assert_not_called()

    def test_allowlisted_by_user_id_runs(self, bridge, mock_engine, monkeypatch):
        monkeypatch.setenv("SKC_BRIDGE_CODE_ENABLED", "1")
        monkeypatch.setenv("SKC_BRIDGE_CODE_ALLOWED_IDS", "7,42,99")
        reply = bridge._handle_code_command(500, 42, "/code repo=skchat fix it")
        assert reply == "ran (mocked)"
        mock_engine.assert_called_once()

    def test_allowlisted_by_chat_id_runs(self, bridge, mock_engine, monkeypatch):
        monkeypatch.setenv("SKC_BRIDGE_CODE_ENABLED", "1")
        monkeypatch.setenv("SKC_BRIDGE_CODE_ALLOWED_IDS", "500")
        reply = bridge._handle_code_command(500, 42, "/code repo=skchat fix it")
        assert reply == "ran (mocked)"
        mock_engine.assert_called_once()


# ── dispatch: mode + repo parsing feed the engine ───────────────────────────


class TestDispatch:
    def test_direct_mode_by_default(self, bridge, mock_engine, monkeypatch):
        monkeypatch.setenv("SKC_BRIDGE_CODE_ENABLED", "1")
        monkeypatch.setenv("SKC_BRIDGE_CODE_ALLOWED_IDS", "42")
        bridge._handle_code_command(42, 42, "/code repo=skchat fix a typo")
        args, kwargs = mock_engine.call_args
        # signature: _run_autocode(repo, task, mode, *, chat_id=...)
        assert args[0] == "skchat"
        assert args[1] == "fix a typo"
        assert args[2] == "direct"

    def test_gated_keyword_selects_gated_executor(self, bridge, mock_engine, monkeypatch):
        monkeypatch.setenv("SKC_BRIDGE_CODE_ENABLED", "1")
        monkeypatch.setenv("SKC_BRIDGE_CODE_ALLOWED_IDS", "42")
        bridge._handle_code_command(42, 42, "/code gated repo=skchat do it")
        args, kwargs = mock_engine.call_args
        assert args[2] == "gated"

    def test_no_repo_refuses_and_never_calls_engine(self, bridge, mock_engine, monkeypatch):
        monkeypatch.setenv("SKC_BRIDGE_CODE_ENABLED", "1")
        monkeypatch.setenv("SKC_BRIDGE_CODE_ALLOWED_IDS", "42")
        monkeypatch.delenv("SKC_BRIDGE_CODE_DEFAULT_REPO", raising=False)
        reply = bridge._handle_code_command(42, 42, "/code just do something")
        assert reply is not None
        assert "repo" in reply.lower()
        mock_engine.assert_not_called()

    def test_empty_task_refuses(self, bridge, mock_engine, monkeypatch):
        monkeypatch.setenv("SKC_BRIDGE_CODE_ENABLED", "1")
        monkeypatch.setenv("SKC_BRIDGE_CODE_ALLOWED_IDS", "42")
        reply = bridge._handle_code_command(42, 42, "/code repo=skchat")
        assert reply is not None
        mock_engine.assert_not_called()


# ── the engine seam: executor selection + the no-automerge guardrail ────────


@dataclass
class _FakeRepoSpec:
    name: str
    automerge: bool = False


class _FakeConfig:
    def __init__(self):
        self.harness = "stub"
        self.automerge_repos = []
        self.repo_map = {"skchat": _FakeRepoSpec("skchat")}

    def repo(self, name):
        return self.repo_map.get(name)


class TestEngineSeam:
    def _patch_engine(self, monkeypatch, direct_cls, eng_cls, cfg):
        import skharness.autocode.config as ac_config
        import skharness.autocode.direct as ac_direct
        import skharness.autocode.engineering as ac_eng
        import skharness.autocode.harness as ac_harness
        import skharness.autocode.journal as ac_journal

        monkeypatch.setattr(ac_config, "load", lambda *a, **k: cfg)
        monkeypatch.setattr(ac_direct, "DirectExecutor", direct_cls)
        monkeypatch.setattr(ac_eng, "EngineeringExecutor", eng_cls)
        monkeypatch.setattr(ac_harness, "build_harness", lambda *a, **k: MagicMock())
        monkeypatch.setattr(ac_journal, "handle", lambda *a, **k: MagicMock())
        import skcapstone.coordination as coord
        import skcapstone.mcp_tools._helpers as helpers

        monkeypatch.setattr(coord, "Board", lambda *a, **k: MagicMock())
        monkeypatch.setattr(helpers, "_shared_root", lambda *a, **k: "/tmp/shared")

    def test_direct_uses_direct_executor(self, bridge, monkeypatch):
        cfg = _FakeConfig()
        direct_cls, eng_cls = MagicMock(), MagicMock()
        self._patch_engine(monkeypatch, direct_cls, eng_cls, cfg)
        bridge._run_autocode("skchat", "fix a typo", "direct", chat_id=42)
        assert direct_cls.called
        assert not eng_cls.called

    def test_gated_uses_engineering_executor(self, bridge, monkeypatch):
        cfg = _FakeConfig()
        direct_cls, eng_cls = MagicMock(), MagicMock()
        self._patch_engine(monkeypatch, direct_cls, eng_cls, cfg)
        bridge._run_autocode("skchat", "do it", "gated", chat_id=42)
        assert eng_cls.called
        assert not direct_cls.called

    def test_gated_does_not_enable_automerge(self, bridge, monkeypatch):
        cfg = _FakeConfig()
        direct_cls, eng_cls = MagicMock(), MagicMock()
        self._patch_engine(monkeypatch, direct_cls, eng_cls, cfg)
        bridge._run_autocode("skchat", "do it", "gated", chat_id=42)
        # the binding loads config read-only: it must never flip automerge on.
        assert cfg.automerge_repos == []
        assert cfg.repo_map["skchat"].automerge is False

    def test_unknown_repo_refuses_without_running(self, bridge, monkeypatch):
        cfg = _FakeConfig()
        direct_cls, eng_cls = MagicMock(), MagicMock()
        self._patch_engine(monkeypatch, direct_cls, eng_cls, cfg)
        reply = bridge._run_autocode("nope", "x", "direct", chat_id=42)
        assert "nope" in reply
        assert not direct_cls.called
        assert not eng_cls.called
