"""Portability tests for the Telegram bridge import-path resolution.

The bridge used to hardcode ``sys.path.insert(0, "/home/cbrd21/...")`` lines,
so it could only start on one machine under one username. The resolution now
lives in ``scripts/bridge_paths.py``: installed packages first, then the
SKCHAT_SRC/SKCOMMS_SRC env overrides, then paths derived from the checkout
layout. These tests exercise that logic on a fake foreign-home layout (a
different username and repo location) with no network and no real sys.path
mutation, plus a full mock-based import of ``scripts/telegram_bridge.py``.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str, modname: str):
    spec = importlib.util.spec_from_file_location(modname, _SCRIPTS / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bp():
    return _load("bridge_paths.py", "bridge_paths_under_test")


def _mkpkg(src_dir: pathlib.Path, package: str) -> pathlib.Path:
    """Create ``src_dir/package/__init__.py`` and return src_dir."""
    pkg = src_dir / package
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    return src_dir


@pytest.fixture()
def foreign(tmp_path):
    """A foreign-home checkout layout: /home/alice/repos/{skchat,skcomms}."""
    repos = tmp_path / "home" / "alice" / "repos"
    skchat_src = _mkpkg(repos / "skchat" / "src", "skchat")
    skcomms_src = _mkpkg(repos / "skcomms" / "src", "skcomms")
    (repos / "skchat" / "scripts").mkdir(parents=True)
    script = repos / "skchat" / "scripts" / "telegram_bridge.py"
    script.write_text("# placeholder\n")
    return types.SimpleNamespace(
        repos=repos, script=script, skchat_src=skchat_src, skcomms_src=skcomms_src
    )


# ── compute_src_paths: pure resolution logic ────────────────────────────────


class TestComputeSrcPaths:
    def test_installed_packages_preferred(self, bp, foreign):
        """Both packages importable (pip install -e) -> nothing to add."""
        out = bp.compute_src_paths(
            script_path=foreign.script, environ={}, importable=lambda p: True
        )
        assert out == []

    def test_foreign_layout_derives_from_checkout(self, bp, foreign):
        """Not installed, no env: derive <repo>/src + sibling ../skcomms/src."""
        out = bp.compute_src_paths(
            script_path=foreign.script, environ={}, importable=lambda p: False
        )
        assert out == [str(foreign.skcomms_src), str(foreign.skchat_src)]

    def test_env_override_wins_over_checkout(self, bp, foreign, tmp_path):
        alt_chat = _mkpkg(tmp_path / "alt" / "skchat" / "src", "skchat")
        alt_comms = _mkpkg(tmp_path / "alt" / "skcomms" / "src", "skcomms")
        env = {"SKCHAT_SRC": str(alt_chat), "SKCOMMS_SRC": str(alt_comms)}
        out = bp.compute_src_paths(
            script_path=foreign.script, environ=env, importable=lambda p: False
        )
        assert out == [str(alt_comms), str(alt_chat)]

    def test_env_override_ignored_when_missing_package(self, bp, foreign, tmp_path):
        """A bogus override (dir has no package) falls back to the checkout."""
        env = {
            "SKCHAT_SRC": str(tmp_path / "nope"),
            "SKCOMMS_SRC": str(tmp_path / "also-nope"),
        }
        out = bp.compute_src_paths(
            script_path=foreign.script, environ=env, importable=lambda p: False
        )
        assert out == [str(foreign.skcomms_src), str(foreign.skchat_src)]

    def test_partial_install_only_adds_the_missing_one(self, bp, foreign):
        """skchat installed, skcomms not: only the skcomms path is added."""
        out = bp.compute_src_paths(
            script_path=foreign.script,
            environ={},
            importable=lambda p: p == "skchat",
        )
        assert out == [str(foreign.skcomms_src)]

    def test_unresolvable_package_adds_nothing(self, bp, tmp_path):
        """No install, no env, no checkout siblings: empty (imports would fail
        later with a normal ImportError instead of a bogus path)."""
        lone = tmp_path / "lonely" / "skchat" / "scripts" / "telegram_bridge.py"
        lone.parent.mkdir(parents=True)
        lone.write_text("# placeholder\n")
        out = bp.compute_src_paths(
            script_path=lone, environ={}, importable=lambda p: False
        )
        assert out == []


# ── ensure_importable: sys.path application ─────────────────────────────────


class TestEnsureImportable:
    def test_insertion_order_matches_legacy(self, bp, foreign):
        """skchat/src must end up AHEAD of skcomms/src, like the old inserts."""
        paths: list[str] = ["/existing"]
        added = bp.ensure_importable(
            script_path=foreign.script,
            environ={},
            importable=lambda p: False,
            path_list=paths,
        )
        assert paths == [str(foreign.skchat_src), str(foreign.skcomms_src), "/existing"]
        assert set(added) == {str(foreign.skchat_src), str(foreign.skcomms_src)}

    def test_idempotent(self, bp, foreign):
        paths: list[str] = []
        bp.ensure_importable(
            script_path=foreign.script, environ={},
            importable=lambda p: False, path_list=paths,
        )
        again = bp.ensure_importable(
            script_path=foreign.script, environ={},
            importable=lambda p: False, path_list=paths,
        )
        assert again == []
        assert len(paths) == 2

    def test_noop_when_installed(self, bp, foreign):
        paths: list[str] = []
        added = bp.ensure_importable(
            script_path=foreign.script, environ={},
            importable=lambda p: True, path_list=paths,
        )
        assert added == [] and paths == []

    def test_repo_root_derivation(self, bp, foreign):
        assert bp.repo_root(foreign.script) == foreign.repos / "skchat"


# ── source audit: the acceptance criterion, as a regression test ────────────


class TestNoHardcodedPaths:
    @pytest.mark.parametrize(
        "script", ["telegram_bridge.py", "bridge_consciousness.py", "bridge_paths.py"]
    )
    def test_no_home_cbrd21(self, script):
        text = (_SCRIPTS / script).read_text()
        assert "/home/cbrd21" not in text, f"{script} hardcodes /home/cbrd21"

    @pytest.mark.parametrize(
        "script", ["telegram_bridge.py", "bridge_consciousness.py", "bridge_paths.py"]
    )
    def test_no_absolute_sys_path_literals(self, script):
        text = (_SCRIPTS / script).read_text()
        assert 'sys.path.insert(0, "/' not in text
        assert "sys.path.insert(0, '/" not in text


# ── full module import (mock-based, no token, no network) ───────────────────


@pytest.fixture()
def stubbed_skcapstone(monkeypatch):
    """Stub skcapstone so importing telegram_bridge touches no daemon/network.

    ``_build_system_prompt`` runs at import time; the real SystemPromptBuilder
    probes the live consciousness daemon. The stub raises, which exercises the
    documented fallback-persona path instead.
    """

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("stubbed for tests")

    fake_cl = types.ModuleType("skcapstone.consciousness_loop")
    fake_cl.SystemPromptBuilder = _Boom
    fake_me = types.ModuleType("skcapstone.memory_engine")
    fake_pkg = types.ModuleType("skcapstone")
    fake_pkg.consciousness_loop = fake_cl
    fake_pkg.memory_engine = fake_me
    monkeypatch.setitem(sys.modules, "skcapstone", fake_pkg)
    monkeypatch.setitem(sys.modules, "skcapstone.consciousness_loop", fake_cl)
    monkeypatch.setitem(sys.modules, "skcapstone.memory_engine", fake_me)


class TestTelegramBridgeImport:
    def test_module_imports_without_hardcoded_paths(
        self, tmp_path, monkeypatch, stubbed_skcapstone
    ):
        """The bridge module loads with only installed/derived packages: no
        token, no network, no /home/cbrd21 assumptions."""
        monkeypatch.setenv("SKC_BRIDGE_TOKEN", "dummy-token-for-tests")
        monkeypatch.setenv("SKC_BRIDGE_AGENT_HOME", str(tmp_path / "agent-home"))
        modname = "telegram_bridge_under_test"
        monkeypatch.delitem(sys.modules, modname, raising=False)
        mod = _load("telegram_bridge.py", modname)
        try:
            # Path resolution ran and recorded what it added (empty in this
            # environment: skchat/skcomms resolve as installed packages).
            assert hasattr(mod, "_SRC_PATHS_ADDED")
            assert isinstance(mod._SRC_PATHS_ADDED, list)
            # The critical imports resolved.
            assert mod.TelegramAdapter is not None
            assert mod.AdapterHub is not None
            # And the token came from env, not a hardcoded value.
            assert mod.TOKEN == "dummy-token-for-tests"
        finally:
            sys.modules.pop(modname, None)

    def test_agent_home_not_machine_specific(
        self, tmp_path, monkeypatch, stubbed_skcapstone
    ):
        """AGENT_HOME honors the env override (foreign-home layout)."""
        foreign_home = tmp_path / "home" / "alice" / ".skcapstone" / "agents" / "opus"
        monkeypatch.setenv("SKC_BRIDGE_TOKEN", "dummy")
        monkeypatch.setenv("SKC_BRIDGE_AGENT_HOME", str(foreign_home))
        modname = "telegram_bridge_under_test_home"
        monkeypatch.delitem(sys.modules, modname, raising=False)
        mod = _load("telegram_bridge.py", modname)
        try:
            assert mod.AGENT_HOME == str(foreign_home)
        finally:
            sys.modules.pop(modname, None)
