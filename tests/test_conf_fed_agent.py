"""Tests for the federated conf-agent join (C4) — skchat/conf/fed_agent.py.

Exercise the "pull the AI agent into a REMOTE-hosted conf" flow WITHOUT any
relay, network, key, or process spawn: the discover / mint / spawn seams are all
injected. We assert:

* token-only flow: discovery (or explicit host) → mint → {token, url} resolved,
* explicit ``host`` skips discovery,
* discovered ``sfu_ws_url`` backfills a missing authd ``url``,
* the spawn command targets the REMOTE SFU (``--url``/``--token``) AND carries
  the creds in the child env (not just argv),
* the systemd unit name is sanitized + distinct (``lumina-fedconf-``),
* failures (discovery error, incomplete token) raise FederatedAgentJoinError
  and NEVER spawn,
* the /conf/{room}/invite-agent-federated route wires it (mocked) end-to-end.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.conf import fed_agent
from skchat.conf.fed_agent import (
    FederatedAgentJoinError,
    fed_agent_unit,
    federated_agent_join,
    mint_federated_agent_token,
)


class _Elected:
    def __init__(self, auth_url, sfu_ws_url):
        self.auth_url = auth_url
        self.sfu_ws_url = sfu_ws_url


class _SpawnRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, env):
        self.calls.append((list(cmd), dict(env)))
        return object()


# ── unit naming ──────────────────────────────────────────────────────────────


def test_fed_agent_unit_sanitizes_and_is_distinct():
    assert fed_agent_unit("stand up!/../x").startswith("lumina-fedconf-")
    assert ".." not in fed_agent_unit("a/../b")
    assert "/" not in fed_agent_unit("a/b")
    # distinct prefix from the LOCAL conf-agent unit
    from skchat.conf.routes import _agent_unit

    assert fed_agent_unit("demo") != _agent_unit("demo")


# ── mint_federated_agent_token ───────────────────────────────────────────────


def test_mint_uses_explicit_host_and_skips_discovery():
    def _discover(_room):
        raise AssertionError("discovery must be skipped when host is given")

    def _mint(auth_url, room, *, fqid=None):
        assert auth_url == "http://box-a:8765"
        return {"token": "JWT", "url": "wss://box-a/lk", "identity": "lumina@chef.skworld"}

    out = mint_federated_agent_token(
        "standup", host="http://box-a:8765", discover=_discover, mint=_mint
    )
    assert out["token"] == "JWT"
    assert out["url"] == "wss://box-a/lk"
    assert out["room"] == "standup"


def test_mint_discovers_when_no_host():
    def _discover(room):
        assert room == "standup"
        return _Elected("http://box-a:8765/conf/standup/federated-token", "wss://box-a/lk")

    def _mint(auth_url, room, *, fqid=None):
        assert auth_url == "http://box-a:8765/conf/standup/federated-token"
        return {"token": "JWT", "url": "wss://box-a/lk"}

    out = mint_federated_agent_token("standup", discover=_discover, mint=_mint)
    assert out["token"] == "JWT"
    assert out["auth_url"].endswith("/federated-token")


def test_mint_backfills_url_from_discovered_sfu():
    def _discover(_room):
        return _Elected("http://box-a:8765/x", "wss://box-a/lk")

    def _mint(auth_url, room, *, fqid=None):
        return {"token": "JWT"}  # authd omitted url

    out = mint_federated_agent_token("standup", discover=_discover, mint=_mint)
    assert out["url"] == "wss://box-a/lk"


def test_mint_raises_on_discovery_failure():
    def _discover(_room):
        raise RuntimeError("no relay")

    with pytest.raises(FederatedAgentJoinError):
        mint_federated_agent_token("standup", discover=_discover, mint=lambda *a, **k: {})


def test_mint_raises_when_no_auth_url_discovered():
    def _discover(_room):
        return _Elected("", "wss://box-a/lk")

    with pytest.raises(FederatedAgentJoinError):
        mint_federated_agent_token("standup", discover=_discover, mint=lambda *a, **k: {})


# ── federated_agent_join (spawn) ─────────────────────────────────────────────


def test_join_spawns_against_remote_sfu_with_env_creds():
    spawn = _SpawnRecorder()
    out = federated_agent_join(
        "standup",
        host="http://box-a:8765",
        mint=lambda a, r, **k: {"token": "JWT", "url": "wss://box-a/lk",
                                "identity": "lumina@chef.skworld", "role": "participant"},
        spawn=spawn,
        lumina_call_script="/opt/lumina/lumina-call.py",
        agent_python="/opt/venv/bin/python",
    )
    assert out["ok"] is True
    assert out["unit"] == fed_agent_unit("standup")
    assert out["url"] == "wss://box-a/lk"
    assert out["identity"] == "lumina@chef.skworld"

    assert len(spawn.calls) == 1
    cmd, env = spawn.calls[0]
    # remote SFU url + token are on argv (CLI fallback) ...
    assert "--url" in cmd and "wss://box-a/lk" in cmd
    assert "--token" in cmd and "JWT" in cmd
    assert "/opt/lumina/lumina-call.py" in cmd
    assert "/opt/venv/bin/python" in cmd
    assert f"--unit={fed_agent_unit('standup')}" in cmd
    # ... AND ride in on the child env (the durable, ps-safe channel).
    assert env["SKCHAT_CONF_TOKEN"] == "JWT"
    assert env["SKCHAT_CONF_URL"] == "wss://box-a/lk"


def test_join_does_not_spawn_on_incomplete_token():
    spawn = _SpawnRecorder()
    with pytest.raises(FederatedAgentJoinError):
        federated_agent_join(
            "standup",
            host="http://box-a:8765",
            mint=lambda a, r, **k: {"token": "", "url": ""},  # incomplete
            spawn=spawn,
        )
    assert spawn.calls == []


def test_join_rejects_overlong_greeting():
    spawn = _SpawnRecorder()
    with pytest.raises(FederatedAgentJoinError):
        federated_agent_join(
            "standup", host="http://x", greet="g" * 501,
            mint=lambda *a, **k: {"token": "J", "url": "u"}, spawn=spawn,
        )
    assert spawn.calls == []


# ── route: /conf/{room}/invite-agent-federated ───────────────────────────────


def _route_client(monkeypatch, tmp_path, *, fake_join, which=True):
    from skchat.conf.room import ConfRegistry
    from skchat.conf.routes import register_conf_routes

    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "s0123456789")
    monkeypatch.setattr(
        "skchat.conf.routes.shutil.which",
        lambda _n: "/usr/bin/systemd-run" if which else None,
    )
    # Patch the federated join the route calls.
    monkeypatch.setattr(fed_agent, "federated_agent_join", fake_join)

    app = FastAPI()
    reg = ConfRegistry(path=tmp_path / "confs.json")
    register_conf_routes(app, registry=reg, runner=lambda cmd: _ok())
    return TestClient(app), reg


class _ok:
    returncode = 0
    stderr = ""
    stdout = ""


def test_invite_agent_federated_route_remote_room(monkeypatch, tmp_path):
    seen = {}

    def fake_join(room, *, host=None, fqid=None, greet=""):
        seen["room"] = room
        seen["host"] = host
        return {"ok": True, "unit": "lumina-fedconf-remote", "room": room,
                "url": "wss://box-a/lk", "identity": "lumina@chef.skworld",
                "role": "participant"}

    client, _reg = _route_client(monkeypatch, tmp_path, fake_join=fake_join)
    r = client.post(
        "/conf/remote-room/invite-agent-federated",
        json={"requester": "anyone@chef.skworld", "host": "http://box-a:8765"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["unit"] == "lumina-fedconf-remote"
    assert seen["room"] == "remote-room"
    assert seen["host"] == "http://box-a:8765"


def test_invite_agent_federated_requires_requester(monkeypatch, tmp_path):
    client, _reg = _route_client(
        monkeypatch, tmp_path, fake_join=lambda *a, **k: {"ok": True}
    )
    r = client.post("/conf/x/invite-agent-federated", json={})
    assert r.status_code == 400


def test_invite_agent_federated_503_without_systemd_run(monkeypatch, tmp_path):
    client, _reg = _route_client(
        monkeypatch, tmp_path, fake_join=lambda *a, **k: {"ok": True}, which=False
    )
    r = client.post(
        "/conf/x/invite-agent-federated",
        json={"requester": "a@chef.skworld"},
    )
    assert r.status_code == 503


def test_invite_agent_federated_502_on_join_failure(monkeypatch, tmp_path):
    def fake_join(room, *, host=None, fqid=None, greet=""):
        raise FederatedAgentJoinError("no focus elected")

    client, _reg = _route_client(monkeypatch, tmp_path, fake_join=fake_join)
    r = client.post(
        "/conf/x/invite-agent-federated",
        json={"requester": "a@chef.skworld"},
    )
    assert r.status_code == 502
