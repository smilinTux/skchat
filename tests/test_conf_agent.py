"""Tests for the on-demand Lumina conf-agent routes (conf/routes.py).

Coord task 34bd409b — "Sovereign Conf Calls: pull in the AI agent".

These exercise the transient agent spawn path WITHOUT ever launching a process:
the systemd-run / systemctl command runner is injected (``runner=`` on
``register_conf_routes``) and records the commands it would have run. We assert:

* ``/conf/{room}/invite-agent`` builds the correct ``systemd-run`` command
  (room sanitized into the ``--unit`` name, ``--room`` / ``--greet`` passed,
  resource scope properties present, configurable script + python honored),
* host-gating is enforced (non-host -> 403),
* ``/conf/{room}/remove-agent`` and ``/conf/{room}/end`` both stop the unit
  (``systemctl --user stop <unit>.scope``),
* a failing runner / missing ``systemd-run`` degrades gracefully (clear 5xx,
  never an unhandled crash).
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.conf.room import ConfRegistry
from skchat.conf.routes import register_conf_routes

_KEY, _SECRET = "test-key", "test-secret-0123456789"
_HOST = "lumina@chef.skworld"


class RecordingRunner:
    """Records every command instead of spawning; returns rc=0 by default."""

    def __init__(self, returncode=0, stderr="", raises=None):
        self.calls = []
        self._rc = returncode
        self._stderr = stderr
        self._raises = raises

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        if self._raises is not None:
            raise self._raises
        return _Proc(self._rc, self._stderr)


class _Proc:
    def __init__(self, returncode, stderr=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


def _make(tmp_path, monkeypatch, runner):
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", _KEY)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", _SECRET)
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", "ws://test-sfu:7880")
    monkeypatch.setenv("SKCHAT_LUMINA_CALL_SCRIPT", "/opt/lumina/lumina-call.py")
    monkeypatch.setenv("SKCHAT_AGENT_PYTHON", "/opt/venv/bin/python")
    # systemd-run must look "available" so the route reaches the runner.
    monkeypatch.setattr("skchat.conf.routes.shutil.which", lambda _name: "/usr/bin/systemd-run")
    app = FastAPI()
    register_conf_routes(app, registry=ConfRegistry(path=tmp_path / "confs.json"), runner=runner)
    return TestClient(app)


def _create(client, slug="standup"):
    r = client.post("/conf/create", json={"host_fqid": _HOST, "title": "Standup", "slug": slug})
    assert r.status_code == 200
    return r.json()["room"]


def test_invite_agent_builds_correct_systemd_run_command(tmp_path, monkeypatch):
    runner = RecordingRunner()
    client = _make(tmp_path, monkeypatch, runner)
    room = _create(client)

    r = client.post(
        f"/conf/{room}/invite-agent",
        json={"requester": _HOST, "greet": "Hello team"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["room"] == room

    # Room (e.g. "conf-abc123") sanitized into the unit name (alnum/dash only).
    expected_unit = f"lumina-conf-{room.replace('_', '-')}"
    assert body["unit"] == expected_unit

    assert len(runner.calls) == 1
    cmd = runner.calls[0]
    assert cmd[0] == "systemd-run"
    assert "--user" in cmd
    assert "--scope" in cmd
    assert f"--unit={expected_unit}" in cmd
    assert "--property=MemoryMax=2G" in cmd
    assert "--property=CPUQuota=200%" in cmd
    # Configurable python + script path honored.
    assert "/opt/venv/bin/python" in cmd
    assert "/opt/lumina/lumina-call.py" in cmd
    # --room <room> and --greet "<greeting>" passed through to the agent.
    assert cmd[cmd.index("--room") + 1] == room
    assert cmd[cmd.index("--greet") + 1] == "Hello team"


def test_invite_agent_sanitizes_room_into_unit_name(tmp_path, monkeypatch):
    """A room with injection-y chars collapses to alnum/dash in the unit name."""
    runner = RecordingRunner()
    client = _make(tmp_path, monkeypatch, runner)
    # Craft a conf whose room contains nasties by overriding the registry record.
    room = _create(client, slug="evil")
    # The real room is "conf-<b32>"; verify the unit name has no shell metachars.
    r = client.post(f"/conf/{room}/invite-agent", json={"requester": _HOST})
    assert r.status_code == 200
    unit = r.json()["unit"]
    import re

    assert re.fullmatch(r"lumina-conf-[A-Za-z0-9-]+", unit)


def test_invite_agent_default_greeting(tmp_path, monkeypatch):
    runner = RecordingRunner()
    client = _make(tmp_path, monkeypatch, runner)
    room = _create(client)
    r = client.post(f"/conf/{room}/invite-agent", json={"requester": _HOST})
    assert r.status_code == 200
    cmd = runner.calls[0]
    # A non-empty default greeting is passed even when none is provided.
    assert cmd[cmd.index("--greet") + 1].strip() != ""


def test_invite_agent_host_gated(tmp_path, monkeypatch):
    runner = RecordingRunner()
    client = _make(tmp_path, monkeypatch, runner)
    room = _create(client)
    r = client.post(f"/conf/{room}/invite-agent", json={"requester": "rando@x.y"})
    assert r.status_code == 403
    # Nothing spawned for an unauthorized requester.
    assert runner.calls == []


def test_invite_agent_unknown_room_404(tmp_path, monkeypatch):
    runner = RecordingRunner()
    client = _make(tmp_path, monkeypatch, runner)
    r = client.post("/conf/conf-nope000000000/invite-agent", json={"requester": _HOST})
    assert r.status_code == 404
    assert runner.calls == []


def test_invite_agent_rejects_overlong_greeting(tmp_path, monkeypatch):
    runner = RecordingRunner()
    client = _make(tmp_path, monkeypatch, runner)
    room = _create(client)
    r = client.post(f"/conf/{room}/invite-agent", json={"requester": _HOST, "greet": "x" * 501})
    assert r.status_code == 400
    assert runner.calls == []


def test_invite_agent_503_when_systemd_run_missing(tmp_path, monkeypatch):
    runner = RecordingRunner()
    client = _make(tmp_path, monkeypatch, runner)
    room = _create(client)
    # systemd-run not on PATH -> clear 503, no crash, no spawn.
    monkeypatch.setattr("skchat.conf.routes.shutil.which", lambda _name: None)
    r = client.post(f"/conf/{room}/invite-agent", json={"requester": _HOST})
    assert r.status_code == 503
    assert runner.calls == []


def test_invite_agent_graceful_error_when_runner_raises(tmp_path, monkeypatch):
    runner = RecordingRunner(raises=FileNotFoundError("systemd-run"))
    client = _make(tmp_path, monkeypatch, runner)
    room = _create(client)
    # Runner blows up (e.g. binary vanished) -> graceful 503, not a 500 crash.
    r = client.post(f"/conf/{room}/invite-agent", json={"requester": _HOST})
    assert r.status_code == 503


def test_invite_agent_502_on_nonzero_returncode(tmp_path, monkeypatch):
    runner = RecordingRunner(returncode=1, stderr="unit already exists")
    client = _make(tmp_path, monkeypatch, runner)
    room = _create(client)
    r = client.post(f"/conf/{room}/invite-agent", json={"requester": _HOST})
    assert r.status_code == 502
    assert "unit already exists" in r.json()["detail"]


def test_remove_agent_stops_unit_host_gated(tmp_path, monkeypatch):
    runner = RecordingRunner()
    client = _make(tmp_path, monkeypatch, runner)
    room = _create(client)

    # non-host cannot remove the agent
    assert (
        client.post(f"/conf/{room}/remove-agent", json={"requester": "rando@x.y"}).status_code
        == 403
    )
    assert runner.calls == []

    r = client.post(f"/conf/{room}/remove-agent", json={"requester": _HOST})
    assert r.status_code == 200
    assert r.json()["stopped"] is True
    # C4: remove-agent now stops BOTH the local (lumina-conf-) and federated
    # (lumina-fedconf-) conf-agent units for the room — idempotent cleanup.
    expected_unit = f"lumina-conf-{room.replace('_', '-')}"
    fed_unit = f"lumina-fedconf-{room.replace('_', '-')}"
    assert runner.calls == [
        ["systemctl", "--user", "stop", f"{expected_unit}.scope"],
        ["systemctl", "--user", "stop", f"{fed_unit}.scope"],
    ]


def test_remove_agent_unknown_room_404(tmp_path, monkeypatch):
    runner = RecordingRunner()
    client = _make(tmp_path, monkeypatch, runner)
    r = client.post("/conf/conf-nope000000000/remove-agent", json={"requester": _HOST})
    assert r.status_code == 404
    assert runner.calls == []


def test_end_stops_agent_unit(tmp_path, monkeypatch):
    runner = RecordingRunner()
    client = _make(tmp_path, monkeypatch, runner)
    room = _create(client)
    r = client.post(f"/conf/{room}/end", json={"requester": _HOST})
    assert r.status_code == 200
    assert r.json()["agent_stopped"] is True
    expected_unit = f"lumina-conf-{room.replace('_', '-')}"
    # end issues a best-effort systemctl stop of the conf-agent scope.
    assert ["systemctl", "--user", "stop", f"{expected_unit}.scope"] in runner.calls


def test_remove_agent_stop_failure_degrades(tmp_path, monkeypatch):
    """A failing systemctl stop is swallowed (best-effort) -> stopped=False, 200."""
    runner = RecordingRunner(raises=RuntimeError("dbus down"))
    client = _make(tmp_path, monkeypatch, runner)
    room = _create(client)
    r = client.post(f"/conf/{room}/remove-agent", json={"requester": _HOST})
    assert r.status_code == 200
    assert r.json()["stopped"] is False


def test_agent_routes_registered_on_app():
    app = FastAPI()
    register_conf_routes(app)
    paths = {r.path for r in app.routes}
    assert "/conf/{room}/invite-agent" in paths
    assert "/conf/{room}/remove-agent" in paths


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
