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


def test_list_spaces_returns_newest_first(client, monkeypatch):
    """GET /spaces sorts at the source (newest created_at on top) so both the
    web directory and the Flutter app inherit the order without re-sorting."""
    import itertools

    import skchat.spaces.routes as routes_mod

    ticks = itertools.count(100.0, 100.0)
    monkeypatch.setattr(routes_mod.time, "time", lambda: next(ticks))

    first = client.post(
        "/spaces/create",
        json={"host_fqid": "lumina@chef.skworld", "title": "Oldest", "slug": "oldest"},
    ).json()["space_id"]
    second = client.post(
        "/spaces/create",
        json={"host_fqid": "lumina@chef.skworld", "title": "Middle", "slug": "middle"},
    ).json()["space_id"]
    third = client.post(
        "/spaces/create",
        json={"host_fqid": "lumina@chef.skworld", "title": "Newest", "slug": "newest"},
    ).json()["space_id"]

    ids = [s["space_id"] for s in client.get("/spaces").json()["spaces"]]
    assert ids == [third, second, first]


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


def test_moderator_prefers_api_url_over_public_funnel_url(tmp_path, monkeypatch):
    """Regression: the server-side Moderator (Twirp RoomService) must NOT be
    built from the browser-facing SKCHAT_LIVEKIT_URL when it carries a Funnel
    path prefix (e.g. wss://host/livekit-ws) - the LiveKit SDK mangles that
    into a double-slash Twirp URL the proxy 404s. A dedicated
    SKCHAT_LIVEKIT_API_URL must win when set."""
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", _KEY)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", _SECRET)
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", "wss://public.example.ts.net/livekit-ws")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_URL", "http://127.0.0.1:7880")

    captured = {}

    class FakeModerator:
        def __init__(self, ws_url, api_key, api_secret):
            captured["ws_url"] = ws_url

        async def stage_action(self, room, identity, action):
            return False

    import skchat.spaces.moderation as moderation_mod

    monkeypatch.setattr(moderation_mod, "Moderator", FakeModerator)

    app = FastAPI()
    register_spaces_routes(app, registry=SpaceRegistry(path=tmp_path / "spaces.json"))
    c = TestClient(app)
    sid = c.post(
        "/spaces/create", json={"host_fqid": "lumina@chef.skworld", "title": "T", "slug": "s"}
    ).json()["space_id"]
    c.post(f"/spaces/{sid}/raise-hand", json={"identity": "alice@x.y"})

    assert captured["ws_url"] == "http://127.0.0.1:7880"


def test_moderator_falls_back_to_public_url_when_api_url_unset(tmp_path, monkeypatch):
    """Backward compat: deployments that never set SKCHAT_LIVEKIT_API_URL keep
    working exactly as before, off SKCHAT_LIVEKIT_URL."""
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", _KEY)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", _SECRET)
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", "ws://test-sfu:7880")
    monkeypatch.delenv("SKCHAT_LIVEKIT_API_URL", raising=False)

    captured = {}

    class FakeModerator:
        def __init__(self, ws_url, api_key, api_secret):
            captured["ws_url"] = ws_url

        async def stage_action(self, room, identity, action):
            return False

    import skchat.spaces.moderation as moderation_mod

    monkeypatch.setattr(moderation_mod, "Moderator", FakeModerator)

    app = FastAPI()
    register_spaces_routes(app, registry=SpaceRegistry(path=tmp_path / "spaces.json"))
    c = TestClient(app)
    sid = c.post(
        "/spaces/create", json={"host_fqid": "lumina@chef.skworld", "title": "T", "slug": "s"}
    ).json()["space_id"]
    c.post(f"/spaces/{sid}/raise-hand", json={"identity": "alice@x.y"})

    assert captured["ws_url"] == "ws://test-sfu:7880"


def _meta(token):
    return jwt.decode(
        token, _SECRET, algorithms=["HS256"], options={"verify_aud": False}
    ).get("metadata")


def test_public_space_join_does_NOT_stamp_fingerprint(client):
    """SECURITY: the unauthenticated /spaces/create + /spaces/{id}/join identity
    is caller-supplied and unproven, so the token must NOT carry a soul_fingerprint
    (else any caller could wear a keyed agent's trust badge by claiming its
    identity). Proven Space joins get their badge via the federation authd path."""
    create = client.post(
        "/spaces/create",
        json={"host_fqid": "lumina@chef.skworld", "title": "T", "slug": "t"},
    )
    assert create.status_code == 200
    assert not _meta(create.json()["token"])  # host token: no metadata claim

    sid = create.json()["space_id"]
    join = client.post(
        f"/spaces/{sid}/join",
        json={"identity": "lumina@chef.skworld", "name": "Impostor"},
    )
    assert join.status_code == 200
    assert not _meta(join.json()["token"])  # a spoof-claim of Lumina carries NO badge
