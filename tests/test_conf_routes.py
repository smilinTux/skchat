"""Tests for the Sovereign Conf Calls REST API (conf/routes.py).

Mirrors tests/test_spaces_routes.py: create -> token -> end with dummy creds (no
live SFU), JWT shape assertions on the minted tokens, graceful roster degradation
without a reachable SFU, and route registration on the app.
"""

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.conf.room import ConfRegistry
from skchat.conf.routes import register_conf_routes

_KEY, _SECRET = "test-key", "test-secret-0123456789"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", _KEY)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", _SECRET)
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", "ws://test-sfu:7880")
    app = FastAPI()
    # inject a tmp-path registry so tests don't touch ~/.skchat
    register_conf_routes(app, registry=ConfRegistry(path=tmp_path / "confs.json"))
    return TestClient(app)


def _video(token):
    return jwt.decode(token, _SECRET, algorithms=["HS256"], options={"verify_aud": False})["video"]


def _create(client, **over):
    body = {"host_fqid": "lumina@chef.skworld", "title": "Standup"}
    body.update(over)
    return client.post("/conf/create", json=body)


def test_create_returns_sovereign_token_and_registers(client):
    r = _create(client, slug="standup")
    assert r.status_code == 200
    body = r.json()
    assert body["conf_id"].startswith("conf-")
    assert body["room"] == body["conf_id"]
    assert body["role"] == "sovereign"
    assert body["join_url"] == f"/conf/{body['room']}"
    # SOVEREIGN host with sovereign_admin=True carries roomAdmin + full publish
    v = _video(body["token"])
    assert v["roomAdmin"] is True
    assert v["canPublish"] is True

    live = client.get("/conf").json()["confs"]
    assert any(c["conf_id"] == body["conf_id"] for c in live)


def test_create_named_slug_is_deterministic(client):
    a = _create(client, slug="weekly").json()["conf_id"]
    b = _create(client, slug="weekly").json()["conf_id"]
    assert a == b  # same (host, slug) -> same room


def test_create_adhoc_without_slug_is_random(client):
    a = _create(client).json()["conf_id"]
    b = _create(client).json()["conf_id"]
    assert a != b  # no slug -> fresh random room each time


def test_create_requires_host_and_title(client):
    assert _create(client, host_fqid="", slug="x").status_code == 400
    assert _create(client, title="", slug="x").status_code == 400


def test_create_rejects_overlong_title(client):
    assert _create(client, title="x" * 121, slug="long").status_code == 400
    assert _create(client, title="x" * 120, slug="ok").status_code == 200


def test_create_503_without_creds(client, monkeypatch):
    monkeypatch.delenv("SKCHAT_LIVEKIT_API_KEY", raising=False)
    monkeypatch.delenv("SKCHAT_LIVEKIT_API_SECRET", raising=False)
    assert _create(client, slug="nocreds").status_code == 503


def test_token_defaults_to_participant_and_is_a_jwt(client):
    room = _create(client, slug="s").json()["room"]
    r = client.post(f"/conf/{room}/token", json={"identity": "opus@chef.skworld", "name": "Opus"})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "participant"
    v = _video(body["token"])  # decodes -> it is a valid signed JWT
    assert v["room"] == room
    assert v["canPublish"] is True
    # PARTICIPANT must NOT be a room admin
    assert v.get("roomAdmin", False) is False


def test_token_honors_explicit_role(client):
    room = _create(client, slug="s").json()["room"]
    r = client.post(
        f"/conf/{room}/token",
        json={"identity": "guest@x.y", "role": "guest_conf"},
    )
    assert r.status_code == 200
    assert r.json()["role"] == "guest_conf"
    # guest can never carry room_admin (factory-enforced)
    assert _video(r.json()["token"]).get("roomAdmin", False) is False


def test_token_rejects_unknown_role(client):
    room = _create(client, slug="s").json()["room"]
    r = client.post(f"/conf/{room}/token", json={"identity": "x@y.z", "role": "emperor"})
    assert r.status_code == 400


def test_token_requires_identity(client):
    room = _create(client, slug="s").json()["room"]
    assert client.post(f"/conf/{room}/token", json={}).status_code == 400


def test_token_unknown_room_404(client):
    r = client.post("/conf/conf-doesnotexist0/token", json={"identity": "x@y.z"})
    assert r.status_code == 404


def test_participants_degrades_gracefully(client):
    """No reachable SFU -> empty roster + live=false, never a 5xx."""
    room = _create(client, slug="s").json()["room"]
    r = client.get(f"/conf/{room}/participants")
    assert r.status_code == 200
    body = r.json()
    assert body["participants"] == []
    assert body["live"] is False


def test_participants_unknown_room_404(client):
    assert client.get("/conf/conf-nope000000000/participants").status_code == 404


def test_end_marks_not_live_host_gated(client):
    room = _create(client, slug="s").json()["room"]
    # non-host cannot end
    assert client.post(f"/conf/{room}/end", json={"requester": "rando@x.y"}).status_code == 403
    # host can end
    r = client.post(f"/conf/{room}/end", json={"requester": "lumina@chef.skworld"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    live = client.get("/conf").json()["confs"]
    assert all(c["conf_id"] != room for c in live)


def test_token_on_ended_conf_404(client):
    room = _create(client, slug="s").json()["room"]
    client.post(f"/conf/{room}/end", json={"requester": "lumina@chef.skworld"})
    r = client.post(f"/conf/{room}/token", json={"identity": "x@y.z"})
    assert r.status_code == 404


def test_end_unknown_room_404(client):
    assert (
        client.post("/conf/conf-nope000000000/end", json={"requester": "x@y.z"}).status_code == 404
    )


def test_routes_registered_on_app():
    app = FastAPI()
    register_conf_routes(app)
    paths = {r.path for r in app.routes}
    assert "/conf/create" in paths
    assert "/conf/{room}/token" in paths
    assert "/conf/{room}/participants" in paths
    assert "/conf/{room}/end" in paths
    assert "/conf" in paths
