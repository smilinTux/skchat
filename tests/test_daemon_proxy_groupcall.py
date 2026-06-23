"""Tests for GROUP A/V CALL support (Phase 3) over the WEB HTTP path.

Everything must work over the same-origin webui HTTP endpoints (the Flutter app
is a web build — CLI is a no-op on web). Contract the Flutter app depends on:

  * POST /api/v1/groups/{id}/call/start  -> mint member-scoped token + room + ring
  * POST /api/v1/groups/{id}/call/join   -> mint member-scoped token + room (no ring)
  * GET  /api/v1/groups/{id}/call/participants -> active participants + roster

Plus the pure model: deterministic room from group id, member-only token mint,
non-members refused.
"""

from __future__ import annotations

import jwt  # PyJWT
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy
from skchat import daemon_proxy_groupcall as GC
from skchat import daemon_proxy_groups as G
from skchat import livekit_routes

_KEY = "test-key"
_SECRET = "test-secret-0123456789abcdef0123456789abcdef"


@pytest.fixture
def client(tmp_path, monkeypatch):
    from skchat.history import ChatHistory

    hist = ChatHistory(store=None, history_dir=tmp_path / "history")
    monkeypatch.setattr(daemon_proxy, "_HISTORY", hist)

    groups_dir = tmp_path / "groups"
    monkeypatch.setattr(G, "_GROUPS_DIR", groups_dir)
    monkeypatch.setattr(G, "resolve_identity", lambda raw: (raw or "").strip())

    # LiveKit creds for the token mint (livekit_routes reads module globals).
    monkeypatch.setattr(livekit_routes, "LIVEKIT_API_KEY", _KEY)
    monkeypatch.setattr(livekit_routes, "LIVEKIT_API_SECRET", _SECRET)

    # Don't actually ring over skcomms in tests — record the calls instead.
    rung_calls = []

    def fake_ring(group, caller, room, sfu_url, *, topic="", **kw):
        for m in group.members:
            if m.identity_uri != caller:
                rung_calls.append((m.identity_uri, room, group.id))
        return [m.identity_uri for m in group.members if m.identity_uri != caller]

    monkeypatch.setattr(GC, "ring_members", fake_ring)

    app = FastAPI()
    app.include_router(daemon_proxy.router)
    tc = TestClient(app)
    tc.rung_calls = rung_calls  # type: ignore[attr-defined]
    return tc


def _create(client, name="Squad", members=None):
    body = {"name": name}
    if members is not None:
        body["members"] = [{"identity": m} for m in members]
    r = client.post("/api/v1/groups", json=body)
    assert r.status_code == 200, r.text
    return r.json()["group_id"]


# ── Pure model ─────────────────────────────────────────────────────────────
def test_room_is_deterministic_from_group_id():
    a = GC.derive_group_room("group-abc")
    b = GC.derive_group_room("group-abc")
    c = GC.derive_group_room("group-xyz")
    assert a == b
    assert a != c
    assert a.startswith("gcall-")
    # 1:1 rooms use a different prefix — no collision.
    assert not a.startswith("call-")


def test_room_does_not_leak_group_id():
    gid = "secret-group-id-12345"
    room = GC.derive_group_room(gid)
    assert gid not in room


def test_mint_token_refuses_non_member(monkeypatch):
    monkeypatch.setattr(livekit_routes, "LIVEKIT_API_KEY", _KEY)
    monkeypatch.setattr(livekit_routes, "LIVEKIT_API_SECRET", _SECRET)
    from skchat.group import GroupChat

    grp = GroupChat.create(name="X", creator_uri="chef@skworld.io")
    grp.add_member(identity_uri="lumina@skworld.io")
    room = GC.derive_group_room(grp.id)
    # Member -> ok.
    tok = GC.mint_member_token(grp, "lumina@skworld.io", room)
    claims = jwt.decode(tok, _SECRET, algorithms=["HS256"], options={"verify_aud": False})
    assert claims["sub"] == "lumina@skworld.io"
    # Token grant is scoped to THIS room and can publish (camera/mic/screen).
    video = claims["video"]
    assert video["room"] == room
    assert video["roomJoin"] is True
    assert video.get("canPublish") is True
    assert not video.get("roomAdmin")
    # Non-member -> refused.
    with pytest.raises(PermissionError):
        GC.mint_member_token(grp, "intruder@evil.io", room)


# ── HTTP path ──────────────────────────────────────────────────────────────
def test_start_call_mints_member_token_and_rings(client):
    gid = _create(client, members=["lumina", "jarvis"])
    r = client.post(f"/api/v1/groups/{gid}/call/start", json={"topic": "standup"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["room"] == GC.derive_group_room(gid)
    assert body["token"]
    # Caller token decodes + is scoped to the derived room.
    claims = jwt.decode(body["token"], _SECRET, algorithms=["HS256"],
                        options={"verify_aud": False})
    assert claims["video"]["room"] == body["room"]
    # Roster includes the operator + the two added members.
    uris = {m["identity_uri"] for m in body["members"]}
    assert "chef@skworld.io" in uris
    assert "lumina" in uris and "jarvis" in uris
    # Everyone but the caller was rung.
    rung = {c[0] for c in client.rung_calls}  # type: ignore[attr-defined]
    assert rung == {"lumina", "jarvis"}
    # Recording seam (Phase 6) is exposed but inert.
    assert body["recording_hook"]["room"] == body["room"]


def test_start_call_no_ring_when_disabled(client):
    gid = _create(client, members=["lumina"])
    r = client.post(f"/api/v1/groups/{gid}/call/start", json={"ring": False})
    assert r.status_code == 200, r.text
    assert client.rung_calls == []  # type: ignore[attr-defined]


def test_join_call_mints_token_without_ringing(client):
    gid = _create(client, members=["lumina"])
    r = client.post(f"/api/v1/groups/{gid}/call/join", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["room"] == GC.derive_group_room(gid)
    assert body["token"]
    assert client.rung_calls == []  # type: ignore[attr-defined] join never rings


def test_call_on_unknown_group_404(client):
    r = client.post("/api/v1/groups/does-not-exist/call/start", json={})
    assert r.status_code == 404


def test_call_503_when_livekit_unconfigured(client, monkeypatch):
    gid = _create(client, members=["lumina"])
    monkeypatch.setattr(livekit_routes, "LIVEKIT_API_KEY", "")
    monkeypatch.setattr(livekit_routes, "LIVEKIT_API_SECRET", "")
    r = client.post(f"/api/v1/groups/{gid}/call/start", json={})
    assert r.status_code == 503


def test_participants_endpoint_returns_room_and_roster(client, monkeypatch):
    gid = _create(client, members=["lumina", "jarvis"])
    # No live SFU in tests -> active list degrades to empty (never 500).
    r = client.get(f"/api/v1/groups/{gid}/call/participants")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["room"] == GC.derive_group_room(gid)
    assert body["active"] == 0
    assert {m["identity_uri"] for m in body["members"]} >= {"lumina", "jarvis"}
