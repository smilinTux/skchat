"""guest-dm: let the operator actually SET a whole-group expiry.

The S3 chokepoint (``_enforce_dm_contact_status``) has always enforced a
``metadata.expires_at`` on a dm-family group: once it passes, every guest of
that room gets a 403 with reason ``group_expired``. Nothing ever wrote it.
``update_group`` accepts name/description/acl only, so the whole branch was
unreachable in practice and the G7 client card had no route to call.

This is the writer: one operator-only PATCH mirroring the per-contact expiry
idiom (``PATCH /guest-dm/contacts/{fp}`` with ``contact_ttl``), so "this room
stops working on Friday" is expressible without revoking anyone individually.
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy
from skchat import daemon_proxy_groups as G
from skchat import guest_group_routes as GGR
from skchat import webui as _webui

_OP = {"X-Operator-Token": "op-secret"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_GUEST_TOKEN_SECRET", "x" * 48)
    monkeypatch.setenv("SKCHAT_GUEST_LINKS_ENABLED", "1")
    monkeypatch.setenv("SKCHAT_GUEST_OPERATOR_TOKEN", "op-secret")
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path / "skchat-home"))
    monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(tmp_path / "rev.db"))
    monkeypatch.setenv("SKCHAT_GUEST_GROUP_DB", str(tmp_path / "gg.db"))
    from skchat import guest as _guest

    _guest._reset_revocation_cache()
    from skchat.history import ChatHistory

    hist = ChatHistory(store=None, history_dir=tmp_path / "history")
    monkeypatch.setattr(daemon_proxy, "_HISTORY", hist)
    monkeypatch.setattr(G, "_GROUPS_DIR", tmp_path / "groups")
    monkeypatch.setattr(G, "resolve_identity", lambda raw: (raw or "").strip())
    return tmp_path


@pytest.fixture
def client(env, monkeypatch):
    async def _bc(msg):
        return None

    monkeypatch.setattr(_webui, "_ws_broadcast", _bc)
    app = FastAPI()
    app.include_router(daemon_proxy.router)
    app.include_router(GGR.router)
    return TestClient(app)


def _auth(session):
    return {"Authorization": f"Bearer {session}"}


def _mint_dm(client, gid="self", **body):
    r = client.post(f"/api/v1/groups/{gid}/invite?mode=dm", json=body, headers=_OP)
    assert r.status_code == 200, r.text
    return r.json()


def _join(client, token, *, name="Alice", pubkey="KEY-A"):
    r = client.post(
        "/api/v1/guest/join",
        json={"invite_token": token, "display_name": name, "guest_pubkey": pubkey},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _patch_expiry(client, gid, body):
    return client.patch(f"/api/v1/guest-dm/groups/{gid}", json=body, headers=_OP)


@pytest.fixture
def room(client):
    inv = _mint_dm(client)
    guest = _join(client, inv["token"])
    return inv["group_id"], guest["session_token"]


def test_group_ttl_is_written_as_an_absolute_expiry(client, room):
    gid, _session = room
    before = time.time()
    r = _patch_expiry(client, gid, {"group_ttl": 3600})
    assert r.status_code == 200, r.text

    expires_at = float(G.load_group(gid).metadata["expires_at"])
    # Stored absolute, not as a TTL, so it does not drift on every read.
    assert before + 3600 <= expires_at <= time.time() + 3600
    assert r.json()["expires_at"] == pytest.approx(expires_at)


def test_an_expired_room_locks_every_guest_out_with_an_honest_reason(client, room):
    gid, session = room
    assert client.get("/api/v1/guest/conversation", headers=_auth(session)).status_code == 200

    assert _patch_expiry(client, gid, {"expires_at": time.time() - 1}).status_code == 200

    r = client.get("/api/v1/guest/conversation", headers=_auth(session))
    assert r.status_code == 403
    # The guest is told the ROOM expired, not that they were revoked - the
    # two are different facts and the client renders them differently.
    assert r.json()["detail"]["reason"] == "group_expired"


def test_clearing_the_expiry_lets_the_room_work_again(client, room):
    gid, session = room
    _patch_expiry(client, gid, {"expires_at": time.time() - 1})
    assert client.get("/api/v1/guest/conversation", headers=_auth(session)).status_code == 403

    r = _patch_expiry(client, gid, {"expires_at": None})
    assert r.status_code == 200
    assert r.json()["expires_at"] is None
    assert G.load_group(gid).metadata.get("expires_at") is None
    assert client.get("/api/v1/guest/conversation", headers=_auth(session)).status_code == 200


def test_a_future_expiry_does_not_lock_anyone_out_yet(client, room):
    gid, session = room
    assert _patch_expiry(client, gid, {"group_ttl": 3600}).status_code == 200
    assert client.get("/api/v1/guest/conversation", headers=_auth(session)).status_code == 200


def test_group_expiry_is_operator_only(client, room):
    gid, _session = room
    r = client.patch(f"/api/v1/guest-dm/groups/{gid}", json={"group_ttl": 60})
    assert r.status_code in (401, 403)
    assert G.load_group(gid).metadata.get("expires_at") is None


def test_a_bad_ttl_is_refused_rather_than_silently_ignored(client, room):
    gid, _session = room
    assert _patch_expiry(client, gid, {"group_ttl": "soon"}).status_code == 400
    assert G.load_group(gid).metadata.get("expires_at") is None


def test_an_unknown_group_404s(client):
    assert _patch_expiry(client, "no-such-group", {"group_ttl": 60}).status_code == 404


def test_expiry_only_applies_to_dm_family_rooms(client, room):
    """The chokepoint only consults `expires_at` for a dm-family group, so
    refuse to set it on a plain group rather than writing a field that would
    silently do nothing."""
    gid, _session = room
    grp = G.load_group(gid)
    grp.metadata.pop("mode", None)  # a plain group, not dm/gdm
    G.save_group(grp)

    assert _patch_expiry(client, gid, {"group_ttl": 60}).status_code == 400
    assert G.load_group(gid).metadata.get("expires_at") is None


def test_the_operator_keeps_their_own_history_after_expiry(client, room):
    """Expiry locks GUESTS out; it is not a delete. The operator's own view of
    the room must survive, or an expiry would quietly destroy their record."""
    gid, session = room
    client.post("/api/v1/guest/send", json={"body": "hello there"}, headers=_auth(session))
    _patch_expiry(client, gid, {"expires_at": time.time() - 1})

    thread = daemon_proxy._get_history().get_thread(gid)
    assert any("hello there" in (getattr(m, "content", "") or "") for m in thread)
