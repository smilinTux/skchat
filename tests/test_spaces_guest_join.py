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
    monkeypatch.setenv("SKCHAT_GUEST_TOKEN_SECRET", "guest-secret-xyz")
    app = FastAPI()
    register_spaces_routes(app, registry=SpaceRegistry(path=tmp_path / "s.json"))
    return TestClient(app)


def test_guest_invite_joins_as_listener_only(client):
    sid = client.post(
        "/spaces/create", json={"host_fqid": "lumina@chef.skworld", "title": "T", "slug": "s"}
    ).json()["space_id"]

    # host mints a guest invite bound to THIS space id (room == space_id)
    from skchat.guest import InviteIssuer

    invite = InviteIssuer().create_invite(
        room=sid, display="Visitor", ttl=3600, issuer="lumina@chef.skworld"
    )
    r = client.post(
        f"/spaces/{sid}/join-guest",
        json={"invite_token": invite["invite_token"], "display": "Visitor"},
    )
    assert r.status_code == 200
    v = jwt.decode(
        r.json()["token"], _SECRET, algorithms=["HS256"], options={"verify_aud": False}
    )["video"]
    assert v.get("canPublish", False) is False  # guest cannot publish
    assert v["canSubscribe"] is True
    assert r.json()["role"] == "listener"


def test_guest_invite_for_other_space_rejected(client):
    sid = client.post(
        "/spaces/create", json={"host_fqid": "lumina@chef.skworld", "title": "T", "slug": "s"}
    ).json()["space_id"]
    from skchat.guest import InviteIssuer

    other = InviteIssuer().create_invite(
        room="space-someotherroom0", display="X", ttl=3600, issuer="lumina@chef.skworld"
    )
    r = client.post(
        f"/spaces/{sid}/join-guest", json={"invite_token": other["invite_token"], "display": "X"}
    )
    assert r.status_code == 403  # invite bound to a different room
