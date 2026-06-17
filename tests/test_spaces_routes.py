import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.spaces.registry import SpaceRegistry
from skchat.spaces.routes import register_spaces_routes

_KEY, _SECRET = "test-key", "test-secret-0123456789"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", _KEY)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", _SECRET)
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", "ws://test-sfu:7880")
    app = FastAPI()
    # inject a tmp-path registry so tests don't touch ~/.skchat
    register_spaces_routes(app, registry=SpaceRegistry(path=tmp_path / "spaces.json"))
    return TestClient(app)


def _video(token):
    return jwt.decode(token, _SECRET, algorithms=["HS256"], options={"verify_aud": False})["video"]


def test_create_returns_host_token_and_registers(client):
    r = client.post(
        "/spaces/create",
        json={"host_fqid": "lumina@chef.skworld", "title": "Town Hall", "slug": "town-hall"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["space_id"].startswith("space-")
    assert body["role"] == "host"
    assert _video(body["token"])["roomAdmin"] is True

    live = client.get("/spaces").json()["spaces"]
    assert any(s["space_id"] == body["space_id"] for s in live)


def test_create_rejects_overlong_title(client):
    """C1 defense-in-depth: cap title length server-side (cheap XSS-blast guard)."""
    r = client.post(
        "/spaces/create",
        json={"host_fqid": "lumina@chef.skworld", "title": "x" * 121, "slug": "long"},
    )
    assert r.status_code == 400
    # a 120-char title is still accepted
    ok = client.post(
        "/spaces/create",
        json={"host_fqid": "lumina@chef.skworld", "title": "x" * 120, "slug": "ok"},
    )
    assert ok.status_code == 200


def test_member_join_gets_listener_token(client):
    sid = client.post(
        "/spaces/create", json={"host_fqid": "lumina@chef.skworld", "title": "T", "slug": "s"}
    ).json()["space_id"]
    r = client.post(f"/spaces/{sid}/join", json={"identity": "opus@chef.skworld", "name": "Opus"})
    assert r.status_code == 200
    v = _video(r.json()["token"])
    assert v.get("canPublish", False) is False
    assert v["canSubscribe"] is True


def test_join_unknown_space_404(client):
    r = client.post("/spaces/space-doesnotexist00/join", json={"identity": "x@y.z"})
    assert r.status_code == 404


def test_end_marks_not_live(client):
    sid = client.post(
        "/spaces/create", json={"host_fqid": "lumina@chef.skworld", "title": "T", "slug": "s"}
    ).json()["space_id"]
    assert (
        client.post(f"/spaces/{sid}/end", json={"requester": "lumina@chef.skworld"}).status_code
        == 200
    )
    live = client.get("/spaces").json()["spaces"]
    assert all(s["space_id"] != sid for s in live)


def test_non_host_cannot_end(client):
    sid = client.post(
        "/spaces/create", json={"host_fqid": "lumina@chef.skworld", "title": "T", "slug": "s"}
    ).json()["space_id"]
    assert client.post(f"/spaces/{sid}/end", json={"requester": "rando@x.y"}).status_code == 403


def test_join_host_mints_host_token_for_host_only(client):
    sid = client.post(
        "/spaces/create", json={"host_fqid": "lumina@chef.skworld", "title": "T", "slug": "s"}
    ).json()["space_id"]
    # host gets a host token (roomAdmin + publish)
    r = client.post(f"/spaces/{sid}/join-host", json={"requester": "lumina@chef.skworld"})
    assert r.status_code == 200
    assert r.json()["role"] == "host"
    assert _video(r.json()["token"])["roomAdmin"] is True
    # non-host is rejected
    bad = client.post(f"/spaces/{sid}/join-host", json={"requester": "rando@x.y"})
    assert bad.status_code == 403
