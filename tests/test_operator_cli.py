"""Tests for the `skchat operator` CLI and its probe module (R2.12).

The operator facet is the canonical explain/observe/act contract Atlas's skchat
adapter mirrors. These tests keep every probe hermetic (injected) so nothing
touches a live daemon, the real outbox, or systemd.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from skchat import operator_probe as op
from skchat.cli import main

# --- explain -----------------------------------------------------------------


def test_explain_shape_matches_contract():
    spec = op.explain()
    assert spec["kinds"] == ["daemon", "bridge", "outbox", "dataplane-auth"]
    assert spec["conditions"] == [
        "DaemonReady",
        "BridgeAlive",
        "OutboxBounded",
        "AuthEnforced",
    ]
    names = {a["name"]: a for a in spec["actions"]}
    assert set(names) == {"restart-daemon", "restart-telegram-bridge", "purge-outbox"}

    # The reversible standard actions.
    for n in ("restart-daemon", "restart-telegram-bridge"):
        a = names[n]
        assert a["standard"] is True
        assert a["reversible"] is True
        assert a["blast_radius"] == "low"

    # purge-outbox: NOT standard, irreversible, blast=delete (forces MAJOR).
    purge = names["purge-outbox"]
    assert purge["standard"] is False
    assert purge["reversible"] is False
    assert purge["blast_radius"] == "delete"


def test_explain_cli_emits_contract_json():
    res = CliRunner().invoke(main, ["operator", "explain"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["conditions"] == op.CONDITIONS


# --- observe -----------------------------------------------------------------


def _conditions(result: dict) -> dict:
    return {c["type"]: c["status"] for c in result["conditions"]}


def test_observe_all_healthy():
    probe = lambda: {  # noqa: E731
        "daemon_ready": True,
        "bridge_alive": True,
        "outbox_depth": 3,
        "outbox_limit": 1000,
        "auth_enforced": True,
    }
    conds = _conditions(op.observe(probe))
    assert conds == {
        "DaemonReady": "True",
        "BridgeAlive": "True",
        "OutboxBounded": "True",
        "AuthEnforced": "True",
    }


def test_observe_daemon_down_fires():
    probe = lambda: {  # noqa: E731
        "daemon_ready": False,
        "bridge_alive": True,
        "outbox_depth": 0,
        "outbox_limit": 1000,
        "auth_enforced": True,
    }
    assert _conditions(op.observe(probe))["DaemonReady"] == "False"


def test_observe_bridge_wedge_fires():
    # The silent-wedge rule: daemon up + poll older than 10 min = wedged.
    probe = lambda: {  # noqa: E731
        "daemon_ready": True,
        "bridge_alive": op._bridge_alive(601, daemon_up=True),
        "outbox_depth": 0,
        "outbox_limit": 1000,
        "auth_enforced": True,
    }
    assert _conditions(op.observe(probe))["BridgeAlive"] == "False"


def test_bridge_wedge_rule_edges():
    # Daemon down -> never wedged (nothing to poll).
    assert op._bridge_alive(9999, daemon_up=False) is True
    # Unknown poll age -> fails safe (alive).
    assert op._bridge_alive(None, daemon_up=True) is True
    # Fresh poll -> alive.
    assert op._bridge_alive(60, daemon_up=True) is True
    # Stale poll while daemon up -> wedged.
    assert op._bridge_alive(601, daemon_up=True) is False


def test_observe_outbox_over_limit_fires():
    probe = lambda: {  # noqa: E731
        "daemon_ready": True,
        "bridge_alive": True,
        "outbox_depth": 1001,
        "outbox_limit": 1000,
        "auth_enforced": True,
    }
    assert _conditions(op.observe(probe))["OutboxBounded"] == "False"


def test_observe_outbox_at_limit_ok():
    probe = lambda: {  # noqa: E731
        "daemon_ready": True,
        "bridge_alive": True,
        "outbox_depth": 1000,
        "outbox_limit": 1000,
        "auth_enforced": True,
    }
    assert _conditions(op.observe(probe))["OutboxBounded"] == "True"


def test_observe_auth_off_fires():
    probe = lambda: {  # noqa: E731
        "daemon_ready": True,
        "bridge_alive": True,
        "outbox_depth": 0,
        "outbox_limit": 1000,
        "auth_enforced": False,
    }
    assert _conditions(op.observe(probe))["AuthEnforced"] == "False"


def test_count_outbox_counts_files(tmp_path):
    (tmp_path / "a.msg").write_text("x")
    (tmp_path / "b.msg").write_text("y")
    (tmp_path / "sub").mkdir()  # dirs are not counted
    assert op._count_outbox(tmp_path) == 2


def test_count_outbox_missing_dir_is_zero(tmp_path):
    assert op._count_outbox(tmp_path / "nope") == 0


def test_observe_cli_healthy_when_unreachable(monkeypatch, tmp_path):
    # No daemon, no bridge heartbeat, empty outbox, auth flag on: all healthy.
    monkeypatch.setenv("SKCHAT_DAEMON_HEALTH", "http://127.0.0.1:1/health")
    monkeypatch.setenv("SKCHAT_BRIDGE_HEARTBEAT", str(tmp_path / "absent.ts"))
    monkeypatch.setenv("SKCOMMS_OUTBOX", str(tmp_path / "empty-outbox"))
    monkeypatch.setenv("SKCHAT_DATAPLANE_AUTH", "1")
    res = CliRunner().invoke(main, ["operator", "observe"])
    assert res.exit_code == 0, res.output
    conds = _conditions(json.loads(res.output))
    assert conds == {
        "DaemonReady": "True",
        "BridgeAlive": "True",
        "OutboxBounded": "True",
        "AuthEnforced": "True",
    }


# --- act ---------------------------------------------------------------------


def test_act_restart_daemon_calls_runner_with_unit():
    calls = []

    def runner(cmd):
        calls.append(cmd)
        return {"ok": True, "returncode": 0}

    result = op.act("restart-daemon", runner=runner)
    assert result["performed"] is True
    assert result["unit"] == "skchat-daemon.service"
    assert calls == [["systemctl", "--user", "restart", "skchat-daemon.service"]]


def test_act_restart_telegram_bridge_uses_agent_unit():
    calls = []

    def runner(cmd):
        calls.append(cmd)
        return {"ok": True, "returncode": 0}

    result = op.act("restart-telegram-bridge", runner=runner, agent="opus")
    assert result["performed"] is True
    assert result["unit"] == "skchat-telegram-opus.service"
    assert calls == [
        ["systemctl", "--user", "restart", "skchat-telegram-opus.service"]
    ]


def test_act_purge_outbox_refuses_and_escalates():
    ran = []
    result = op.act("purge-outbox", runner=lambda cmd: ran.append(cmd))
    assert result["performed"] is False
    assert result["escalate"] == "MAJOR"
    assert "irreversible" in result["reason"].lower()
    assert ran == []  # never actuates


def test_act_unknown_action_refused():
    with pytest.raises(ValueError):
        op.act("nuke-everything", runner=lambda cmd: None)


def test_act_cli_purge_outbox_reports_escalation():
    res = CliRunner().invoke(main, ["operator", "act", "purge-outbox"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["performed"] is False
    assert payload["escalate"] == "MAJOR"


def test_act_cli_unknown_action_errors():
    res = CliRunner().invoke(main, ["operator", "act", "bogus"])
    assert res.exit_code != 0
    assert "unknown" in res.output.lower()
