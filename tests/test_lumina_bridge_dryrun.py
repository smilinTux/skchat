"""Tests for the lumina-bridge --dry-run flag.

The bridge script lives at scripts/lumina-bridge.py (hyphenated, so it is
loaded via importlib rather than a normal import). --dry-run runs the
consciousness loop but must NOT actually deliver replies — send_reply logs
what it WOULD send instead of calling deliver_reply_to_inbox.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_BRIDGE_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "lumina-bridge.py"
)


@pytest.fixture()
def bridge():
    """Load scripts/lumina-bridge.py as a fresh module.

    Reset DRY_RUN to False after each test so module state never leaks.
    """
    spec = importlib.util.spec_from_file_location("lumina_bridge_under_test", _BRIDGE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    mod.DRY_RUN = False


class TestDryRunFlag:
    """Tests for the dry-run send-path gating."""

    def test_dry_run_flag_exists_and_defaults_false(self, bridge) -> None:
        """The module exposes a DRY_RUN flag defaulting to False."""
        assert hasattr(bridge, "DRY_RUN")
        assert bridge.DRY_RUN is False

    def test_normal_mode_calls_delivery(self, bridge, monkeypatch) -> None:
        """In normal mode send_reply delivers via deliver_reply_to_inbox."""
        calls = []
        monkeypatch.setattr(
            bridge,
            "deliver_reply_to_inbox",
            lambda **kw: calls.append(kw),
        )
        bridge.DRY_RUN = False
        bridge.send_reply(
            {"sender": "capauth:opus@skworld.io", "message_id": "m1", "thread_id": "t1"},
            "hello from lumina",
        )
        assert len(calls) == 1
        assert calls[0]["recipient"] == "capauth:opus@skworld.io"
        assert calls[0]["reply_text"] == "hello from lumina"

    def test_dry_run_skips_delivery(self, bridge, monkeypatch) -> None:
        """In dry-run mode send_reply does NOT call deliver_reply_to_inbox."""
        calls = []
        monkeypatch.setattr(
            bridge,
            "deliver_reply_to_inbox",
            lambda **kw: calls.append(kw),
        )
        bridge.DRY_RUN = True
        bridge.send_reply(
            {"sender": "capauth:opus@skworld.io", "message_id": "m1", "thread_id": "t1"},
            "hello from lumina",
        )
        assert calls == []

    def test_dry_run_logs_what_it_would_send(self, bridge, monkeypatch, caplog) -> None:
        """Dry-run logs the intended reply so operators can see it."""
        monkeypatch.setattr(
            bridge,
            "deliver_reply_to_inbox",
            lambda **kw: (_ for _ in ()).throw(AssertionError("should not deliver")),
        )
        bridge.DRY_RUN = True
        with caplog.at_level("INFO"):
            bridge.send_reply(
                {"sender": "capauth:opus@skworld.io", "message_id": "m1"},
                "preview reply",
            )
        combined = " ".join(r.getMessage() for r in caplog.records).lower()
        assert "dry-run" in combined or "dry run" in combined
        assert "preview reply" in " ".join(r.getMessage() for r in caplog.records)

    def test_arg_parser_sets_dry_run(self, bridge) -> None:
        """The CLI arg parser exposes --dry-run and turns it on."""
        parser = bridge._build_arg_parser()
        args = parser.parse_args(["--dry-run"])
        assert args.dry_run is True
        # default is off
        assert parser.parse_args([]).dry_run is False
