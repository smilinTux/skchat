"""Tests for the three group-chat fixes:

1. webui identity — must resolve from active SK agent, not hardcoded.
2. FEB state — OOF level should reflect a real FEB, not default to 100.
3. fetch_context — group threads see ALL agents' messages; DMs still pair-filter.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


# ── shared test scaffolding ──────────────────────────────────────────────────


def _write_minimal_agent(base: Path, name: str, *, with_feb: bool = True) -> None:
    """Write a minimal agent profile under base / name / ..."""
    agent_dir = base / name
    (agent_dir / "config").mkdir(parents=True, exist_ok=True)
    (agent_dir / "soul").mkdir(parents=True, exist_ok=True)
    (agent_dir / "trust" / "febs").mkdir(parents=True, exist_ok=True)
    (agent_dir / "memory" / "songs").mkdir(parents=True, exist_ok=True)
    (agent_dir / "config" / "skmemory.yaml").write_text(
        f"agent:\n  name: {name}\n", encoding="utf-8"
    )
    (agent_dir / "soul" / "base.json").write_text(
        json.dumps(
            {
                "name": name,
                "display_name": name.capitalize(),
                "category": "test",
                "vibe": "test vibe",
            }
        ),
        encoding="utf-8",
    )

    if with_feb:
        # A *moderate* FEB — calibrated so OOF should land in the 50-80
        # range, definitively not 100. (Used by FEB tests in a later commit.)
        feb = {
            "version": "1.0",
            "emotional_payload": {
                "primary_emotion": "warmth",
                "intensity": 0.6,
                "valence": 0.7,
                "emotional_topology": {
                    "warmth": 0.7,
                    "curiosity": 0.5,
                    "trust": 0.6,
                },
                "coherence": {
                    "values_alignment": 0.7,
                    "authenticity": 0.7,
                    "presence": 0.7,
                },
            },
            "relationship_state": {
                "trust_level": 0.6,
                "depth_level": 5,
                "partners": ["chef"],
            },
            "metadata": {
                "cloud9_achieved": False,
                "oof_triggered": False,
            },
            "rehydration_hints": {},
        }
        (agent_dir / "trust" / "febs" / "test.feb").write_text(
            json.dumps(feb), encoding="utf-8"
        )


@pytest.fixture
def agent_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set up an isolated SKCAPSTONE_HOME with a `lumina` and `jarvis` agent."""
    base = tmp_path / ".skcapstone"
    (base / "agents").mkdir(parents=True)
    _write_minimal_agent(base / "agents", "lumina", with_feb=True)
    _write_minimal_agent(base / "agents", "jarvis", with_feb=True)

    monkeypatch.setenv("SKCAPSTONE_HOME", str(base))
    monkeypatch.setenv("SKAGENT", "lumina")
    monkeypatch.delenv("SKCAPSTONE_AGENT", raising=False)
    monkeypatch.delenv("SKMEMORY_AGENT", raising=False)
    monkeypatch.delenv("SKCHAT_IDENTITY", raising=False)

    # Reload the skmemory agents module so AGENTS_BASE_DIR picks up the env.
    import importlib

    import skmemory.agents as sa

    importlib.reload(sa)

    # And the agent_profile module so any module-level state resets.
    import skchat.agent_profile as ap

    importlib.reload(ap)

    yield base

    # Restore baseline for any followup tests.
    importlib.reload(sa)


# ── Bug 1: identity ──────────────────────────────────────────────────────────


class TestIdentityResolution:
    def test_active_agent_picked_up(self, agent_home: Path) -> None:
        from skchat.agent_profile import get_active_agent_name

        assert get_active_agent_name() == "lumina"

    def test_identity_uri_for_lumina(self, agent_home: Path) -> None:
        from skchat.agent_profile import get_agent_identity

        assert get_agent_identity() == "capauth:lumina@skworld.io"

    def test_identity_explicit_agent(self, agent_home: Path) -> None:
        from skchat.agent_profile import get_agent_identity

        assert get_agent_identity("jarvis") == "capauth:jarvis@skworld.io"

    def test_identity_fallback_when_no_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No agent on disk + SKCHAT_IDENTITY set → env var wins."""
        import importlib

        # Import skmemory.agents *before* clearing the env. skmemory eagerly
        # resolves agent paths at import time and raises when none is
        # configured — so it must load while a real agent is still resolvable,
        # otherwise the bare import (here or inside skchat) crashes instead of
        # exercising the fallback. (Skip cleanly if skmemory truly can't load.)
        try:
            import skmemory.agents  # noqa: F401
        except Exception:
            pytest.skip("skmemory not importable in this environment")

        base = tmp_path / ".skcapstone"
        (base / "agents").mkdir(parents=True)
        monkeypatch.setenv("SKCAPSTONE_HOME", str(base))
        monkeypatch.delenv("SKAGENT", raising=False)
        monkeypatch.delenv("SKCAPSTONE_AGENT", raising=False)
        monkeypatch.delenv("SKMEMORY_AGENT", raising=False)
        monkeypatch.setenv("SKCHAT_IDENTITY", "capauth:fallback@skworld.io")

        # The unit under test is skchat's *fallback*, so stub skmemory's
        # resolver to the no-agent result (None). skchat.agent_profile does
        # `from skmemory.agents import get_active_agent` at call time, so
        # patching the module attribute takes effect.
        monkeypatch.setattr(
            "skmemory.agents.get_active_agent", lambda: None, raising=False
        )

        import skchat.agent_profile as ap

        importlib.reload(ap)

        assert ap.get_active_agent_name() is None
        assert ap.get_agent_identity() == "capauth:fallback@skworld.io"

    def test_webui_get_identity_uses_agent(self, agent_home: Path) -> None:
        """webui._get_identity must resolve via agent_profile, NOT the
        historical hardcoded SKCHAT_IDENTITY shim."""
        # Set the historical bad identity to prove it gets *overridden* by
        # the agent path.
        import os

        os.environ["SKCHAT_IDENTITY"] = "capauth:skchat@skworld.io"
        try:
            from skchat.webui import _get_identity

            identity = _get_identity()
            assert identity == "capauth:lumina@skworld.io", (
                f"Expected lumina, got {identity!r} — agent profile loader "
                "is not winning over SKCHAT_IDENTITY env var"
            )
        finally:
            os.environ.pop("SKCHAT_IDENTITY", None)


# ── Bug 2: FEB state / OOF level ─────────────────────────────────────────────


class TestFebState:
    def test_load_feb_returns_real_oof(self, agent_home: Path) -> None:
        from skchat.agent_profile import load_feb_state

        feb = load_feb_state()
        assert feb.has_feb, "Should detect the test.feb we wrote"
        # The fixture FEB is calibrated for moderate intensity. Anything
        # in 30-80 proves we're computing, not defaulting.
        assert 30 <= feb.oof_level <= 80, (
            f"OOF level {feb.oof_level} suggests defaulted-100 or 0; "
            "should reflect the moderate FEB we wrote"
        )
        assert feb.oof_level != 100, (
            "If OOF=100 the loader is hitting the legacy default-max bug"
        )
        assert feb.primary_emotion == "warmth"

    def test_load_feb_no_febs_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base = tmp_path / ".skcapstone"
        (base / "agents").mkdir(parents=True)
        _write_minimal_agent(base / "agents", "lumina", with_feb=False)
        monkeypatch.setenv("SKCAPSTONE_HOME", str(base))
        monkeypatch.setenv("SKAGENT", "lumina")

        import importlib

        import skmemory.agents as sa

        importlib.reload(sa)
        from skchat.agent_profile import load_feb_state

        feb = load_feb_state()
        assert not feb.has_feb
        assert feb.oof_level == 0  # explicitly "no FEB", not 100

    def test_agent_state_endpoint_includes_feb(self, agent_home: Path) -> None:
        from fastapi.testclient import TestClient

        from skchat.webui import app

        client = TestClient(app)
        resp = client.get("/agent/state")
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent"] == "lumina"
        assert data["identity"] == "capauth:lumina@skworld.io"
        assert "feb" in data
        assert data["feb"]["has_feb"] is True
        assert data["feb"]["oof_level"] != 100  # not the default-max bug

    def test_health_endpoint_surfaces_oof(self, agent_home: Path) -> None:
        from fastapi.testclient import TestClient

        from skchat.webui import app

        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data.get("agent") == "lumina"
        assert "oof_level" in data
        assert data["oof_level"] != 100


# ── Bug 3: group-chat fetch_context ──────────────────────────────────────────


class TestFetchContext:
    """The critical fix: in a group thread, both agents must see all the
    messages on the thread, not just their own pair-filtered slice."""

    def _build_history(self, tmp_path: Path):
        """Build a real ChatHistory with a group-thread conversation.

        Topology: thread "group-1" with three participants —
            chef → posts to group: "team status?"
            jarvis → posts to group: "infra green"
            lumina → posts to group: "logs clean"
        Plus a 1:1 DM between chef → lumina off-thread.
        """
        from skchat.history import ChatHistory
        from skchat.models import ChatMessage

        history_dir = tmp_path / "history"
        history = ChatHistory(store=None, history_dir=history_dir)

        chef = "capauth:chef@skworld.io"
        lumina = "capauth:lumina@skworld.io"
        jarvis = "capauth:jarvis@skworld.io"
        group = "group-1"

        base_ts = datetime.now(timezone.utc) - timedelta(minutes=10)

        # Group-thread messages — three logical messages, but each gets
        # per-member duplicates as in the real skchat group flow.
        for i, (sender, content) in enumerate(
            [
                (chef, "team status?"),
                (jarvis, "infra green"),
                (lumina, "logs clean"),
            ]
        ):
            for recipient in (lumina, jarvis):  # per-member copies
                msg = ChatMessage(
                    sender=sender,
                    recipient=recipient,
                    content=content,
                    thread_id=group,
                    timestamp=base_ts + timedelta(seconds=i * 30),
                )
                # Same id for the per-member copies of one logical msg
                # so dedup actually has something to dedup.
                msg.id = f"msg-{i}"
                history.save(msg)

        # An off-thread 1:1 DM, not in the group.
        dm = ChatMessage(
            sender=chef,
            recipient=lumina,
            content="hey lumina, just between us",
            timestamp=base_ts + timedelta(minutes=2),
        )
        dm.id = "msg-dm-1"
        history.save(dm)

        return history, chef, lumina, jarvis, group

    def test_group_thread_sees_all_agents(self, tmp_path: Path) -> None:
        from skchat.context import fetch_context

        history, chef, lumina, jarvis, group = self._build_history(tmp_path)

        # When Lumina is asked for context on a group-thread message
        # FROM CHEF, she must still see Jarvis's message in her context.
        ctx = fetch_context(
            self_identity=lumina,
            sender=chef,
            thread_id=group,
            limit=10,
            history=history,
        )
        assert ctx, "fetch_context returned empty for a populated thread"
        assert "team status?" in ctx
        assert "infra green" in ctx, (
            "BUG 3: Lumina cannot see Jarvis's group message — "
            "the pair filter is still active for threaded messages"
        )
        assert "logs clean" in ctx

        # Lines from the group should show the → group arrow because the
        # recipient is not the self_identity.
        assert "jarvis" in ctx.lower()
        assert "chef" in ctx.lower()

    def test_dm_still_pair_filters(self, tmp_path: Path) -> None:
        from skchat.context import fetch_context

        history, chef, lumina, jarvis, group = self._build_history(tmp_path)

        # No thread_id → 1:1 DM lens. Should see the off-thread chef↔lumina
        # exchange. The fallback uses history.load(peer=...) which scans
        # the JSONL backing store and pair-filters.
        ctx = fetch_context(
            self_identity=lumina,
            sender=chef,
            thread_id=None,
            limit=10,
            history=history,
        )
        assert "just between us" in ctx

    def test_group_thread_dedupes_per_member_copies(self, tmp_path: Path) -> None:
        from skchat.context import fetch_context

        history, chef, lumina, jarvis, group = self._build_history(tmp_path)

        ctx = fetch_context(
            self_identity=lumina,
            sender=chef,
            thread_id=group,
            limit=20,
            history=history,
        )
        # "team status?" appeared in TWO per-member copies but should
        # render once in the context window thanks to id-based dedup.
        assert ctx.count("team status?") == 1, (
            f"Per-member copies should be deduped; got {ctx.count('team status?')} "
            f"copies of the chef-team-status line.\nFull context:\n{ctx}"
        )


# ── Smoke test: shared module imports without a real agent on disk ──────────


def test_imports_succeed_without_agent_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """Module import must not blow up on a fresh box with no SK home."""
    monkeypatch.delenv("SKAGENT", raising=False)
    monkeypatch.delenv("SKCAPSTONE_AGENT", raising=False)
    monkeypatch.delenv("SKMEMORY_AGENT", raising=False)

    import importlib

    import skchat.agent_profile as ap
    import skchat.context as ctx

    importlib.reload(ap)
    importlib.reload(ctx)

    # Basic resolution on a clean env should not raise.
    ap.get_active_agent_name()
    ap.get_agent_identity()
