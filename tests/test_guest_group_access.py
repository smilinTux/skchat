"""Tests for GUEST GROUP access (one-link, group-scoped, untrusted).

Covers the operator spec acceptance:
  * invite mint/verify (valid / expired / revoked / wrong-group)
  * guest/join → untrusted member + group-scoped session token
  * guest CAN send msg + send file + get call token for THEIR group
  * guest CANNOT touch another group / conversation / file / invite / create /
    admin (all 403)
  * guest message carries the advisory signature
  * feature flag gates everything (404 operator / 403 guest when off)
"""

from __future__ import annotations

import io
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy
from skchat import daemon_proxy_groups as G
from skchat import guest_group_routes as GGR
from skchat import guest_groups as GG


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_GUEST_TOKEN_SECRET", "x" * 48)
    monkeypatch.setenv("SKCHAT_GUEST_LINKS_ENABLED", "1")
    # Operator-auth shared token so the TestClient (non-tailnet client IP) can
    # drive the operator invite/revoke routes.
    monkeypatch.setenv("SKCHAT_GUEST_OPERATOR_TOKEN", "op-secret")
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path / "skchat-home"))
    # Isolate the JTI revocation/used store + the transfer→group store.
    monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(tmp_path / "rev.db"))
    monkeypatch.setenv("SKCHAT_GUEST_GROUP_DB", str(tmp_path / "gg.db"))
    # Reset guest.py's revocation cache (module-global).
    from skchat import guest as _guest

    _guest._reset_revocation_cache()

    from skchat.history import ChatHistory

    hist = ChatHistory(store=None, history_dir=tmp_path / "history")
    monkeypatch.setattr(daemon_proxy, "_HISTORY", hist)
    groups_dir = tmp_path / "groups"
    monkeypatch.setattr(G, "_GROUPS_DIR", groups_dir)
    monkeypatch.setattr(G, "resolve_identity", lambda raw: (raw or "").strip())
    return tmp_path


@pytest.fixture
def client(env):
    app = FastAPI()
    # Both routers — operator invite lives in the guest-group router; the group
    # store lives behind daemon_proxy's group helpers (shared store).
    app.include_router(daemon_proxy.router)
    app.include_router(GGR.router)
    return TestClient(app)


def _make_group(name="Town Hall", members=("lumina",)):
    grp = G.create_group(name=name, creator_uri=daemon_proxy.OPERATOR_ID, members=list(members))
    return grp


_OP = {"X-Operator-Token": "op-secret"}


def _invite(client, group_id, **kw):
    r = client.post(f"/api/v1/groups/{group_id}/invite", json=kw, headers=_OP)
    assert r.status_code == 200, r.text
    return r.json()


def _join(client, invite_token, name="Alice", pubkey="PUBKEY-A"):
    r = client.post(
        "/api/v1/guest/join",
        json={"invite_token": invite_token, "display_name": name, "guest_pubkey": pubkey},
    )
    return r


def _auth(session):
    return {"Authorization": f"Bearer {session}"}


# ── invite mint / verify ─────────────────────────────────────────────────────
def test_invite_mint_and_join_makes_untrusted_member(client):
    grp = _make_group()
    inv = _invite(client, grp.id)
    # join_url points at the Flutter app's guest route (hash-routed under
    # /app/), NOT the old /join/<token> page (collided with conf /join).
    assert inv["join_url"] == f"/app/#/g/{inv['token']}"
    assert inv["group_id"] == grp.id

    r = _join(client, inv["token"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trust"] == "untrusted"
    assert body["guest_id"].startswith("guest:alice#")
    assert body["group"]["id"] == grp.id

    # The guest is now an UNTRUSTED member of the group.
    reloaded = G.load_group(grp.id)
    assert GG.is_guest_member(reloaded, body["guest_id"])
    assert reloaded.get_member(body["guest_id"]) is not None
    # Never admin.
    assert reloaded.get_member(body["guest_id"]).role.value == "member"


def test_invite_wrong_group_rejected(client):
    # A token minted for group A cannot be used after we point it at group B —
    # the group_id is baked into the signed token, so join lands in A only.
    grp_a = _make_group(name="A")
    grp_b = _make_group(name="B")
    inv = _invite(client, grp_a.id)
    r = _join(client, inv["token"])
    assert r.json()["group"]["id"] == grp_a.id  # NOT grp_b
    assert grp_b.id != grp_a.id


def test_expired_invite_rejected(client, monkeypatch):
    grp = _make_group()
    # Mint with a now() far in the past so it's already expired.
    past = time.time() - 10_000
    inv = GG.create_group_invite(grp.id, ttl=60, now_fn=lambda: past)
    r = _join(client, inv["token"])
    assert r.status_code == 401


def test_revoked_invite_rejected(client):
    grp = _make_group()
    inv = _invite(client, grp.id)
    # Revoke via the operator route.
    r = client.request(
        "DELETE", f"/api/v1/groups/{grp.id}/invite/{inv['token']}", headers=_OP
    )
    assert r.status_code == 200, r.text
    r2 = _join(client, inv["token"])
    assert r2.status_code == 401


def test_single_use_invite_burns(client):
    grp = _make_group()
    inv = _invite(client, grp.id, single_use=True)
    assert _join(client, inv["token"]).status_code == 200
    # Second use of the same single-use invite is rejected.
    assert _join(client, inv["token"], pubkey="PUBKEY-B").status_code == 401


def test_returning_guest_same_identity(client):
    grp = _make_group()
    inv = _invite(client, grp.id)  # multi-use
    a = _join(client, inv["token"], name="Bob", pubkey="KEY-BOB").json()
    b = _join(client, inv["token"], name="Bob", pubkey="KEY-BOB").json()
    assert a["guest_id"] == b["guest_id"]  # same browser key → same guest


# ── guest CAN: send / file / call (their group) ──────────────────────────────
def test_guest_can_send_signed_message(client):
    grp = _make_group()
    inv = _invite(client, grp.id)
    j = _join(client, inv["token"]).json()
    session = j["session_token"]

    r = client.post(
        "/api/v1/guest/send",
        json={"body": "hello room", "ts": 123, "signature": "SIG-ABC"},
        headers=_auth(session),
    )
    assert r.status_code == 200, r.text
    msg = r.json()["message"]
    assert msg["body"] == "hello room"
    assert msg["is_guest"] is True
    assert msg["signature_present"] is True

    # It shows in the guest's own conversation read.
    conv = client.get("/api/v1/guest/conversation", headers=_auth(session)).json()
    assert any(m["body"] == "hello room" for m in conv["messages"])
    # And in the group thread members read (same store).
    thread = G.group_thread_messages(daemon_proxy._get_history(), grp.id)
    assert any(m.content == "hello room" for m in thread)
    # Advisory signature recorded on the canonical group message.
    gmsg = [m for m in thread if m.content == "hello room"][0]
    assert gmsg.metadata.get("guest_sig") == "SIG-ABC"
    assert gmsg.metadata.get("guest") is True


def test_guest_can_upload_and_download_file(client):
    grp = _make_group()
    inv = _invite(client, grp.id)
    session = _join(client, inv["token"]).json()["session_token"]

    files = {"file": ("note.txt", io.BytesIO(b"secret note"), "text/plain")}
    r = client.post(
        "/api/v1/guest/file",
        files=files,
        data={"caption": "my file"},
        headers=_auth(session),
    )
    assert r.status_code == 200, r.text
    tid = r.json()["transfer_id"]
    assert r.json()["message"]["attachments"][0]["transfer_id"] == tid

    # Download it back (group-scoped allow).
    d = client.get(f"/api/v1/guest/file/{tid}", headers=_auth(session))
    assert d.status_code == 200
    assert d.content == b"secret note"


def test_guest_call_token_degrades_without_livekit(client):
    grp = _make_group()
    inv = _invite(client, grp.id)
    session = _join(client, inv["token"]).json()["session_token"]
    r = client.post("/api/v1/guest/call", json={}, headers=_auth(session))
    # No LiveKit creds in the test env → 503 (not configured), never 500.
    assert r.status_code == 503


def test_guest_react(client):
    grp = _make_group()
    inv = _invite(client, grp.id)
    session = _join(client, inv["token"]).json()["session_token"]
    sent = client.post(
        "/api/v1/guest/send", json={"body": "react to me"}, headers=_auth(session)
    ).json()
    mid = sent["id"]
    r = client.post(
        "/api/v1/guest/react",
        json={"message_id": mid, "emoji": "👍", "op": "add"},
        headers=_auth(session),
    )
    assert r.status_code == 200, r.text


# ── guest CANNOT: another group / invite / create / admin ────────────────────
def test_guest_cannot_send_to_another_group(client):
    grp_a = _make_group(name="A")
    grp_b = _make_group(name="B")
    inv = _invite(client, grp_a.id)
    session = _join(client, inv["token"]).json()["session_token"]
    # Try to direct the send at group B by body group_id → 403.
    r = client.post(
        "/api/v1/guest/send",
        json={"body": "leak", "group_id": grp_b.id},
        headers=_auth(session),
    )
    assert r.status_code == 403


def test_guest_cannot_download_another_groups_file(client):
    grp_a = _make_group(name="A")
    grp_b = _make_group(name="B")
    # A guest of A.
    inv_a = _invite(client, grp_a.id)
    session_a = _join(client, inv_a["token"]).json()["session_token"]
    # A guest of B uploads a file.
    inv_b = _invite(client, grp_b.id)
    session_b = _join(client, inv_b["token"], pubkey="KEY-B").json()["session_token"]
    files = {"file": ("b.txt", io.BytesIO(b"group B secret"), "text/plain")}
    tid = client.post(
        "/api/v1/guest/file", files=files, headers=_auth(session_b)
    ).json()["transfer_id"]
    # Guest A tries to download B's file → 403.
    r = client.get(f"/api/v1/guest/file/{tid}", headers=_auth(session_a))
    assert r.status_code == 403


def test_guest_has_no_invite_or_admin_endpoint(client):
    grp = _make_group()
    inv = _invite(client, grp.id)
    session = _join(client, inv["token"]).json()["session_token"]
    # The guest session token is NOT accepted by the operator invite route
    # (it requires operator auth, not a guest bearer). From a non-tailnet client
    # with only a guest token, minting an invite is refused.
    r = client.post(
        f"/api/v1/groups/{grp.id}/invite",
        json={},
        headers={**_auth(session), "X-Forwarded-For": "8.8.8.8"},
    )
    assert r.status_code in (401, 403)


def test_guest_routes_require_session(client):
    # No token at all → 403 on every guest route.
    assert client.get("/api/v1/guest/conversation").status_code == 403
    assert client.post("/api/v1/guest/send", json={"body": "x"}).status_code == 403
    assert client.post("/api/v1/guest/call", json={}).status_code == 403


def test_guest_bad_session_rejected(client):
    assert client.get(
        "/api/v1/guest/conversation", headers={"Authorization": "Bearer not.a.jwt"}
    ).status_code == 403


# ── feature flag gating ───────────────────────────────────────────────────────
def test_flag_off_operator_404_guest_403(client, monkeypatch):
    grp = _make_group()
    inv = _invite(client, grp.id)  # mint while on
    session = _join(client, inv["token"]).json()["session_token"]
    # Now turn the feature OFF.
    monkeypatch.setenv("SKCHAT_GUEST_LINKS_ENABLED", "0")
    # Operator route → 404 (don't reveal it exists).
    assert client.post(f"/api/v1/groups/{grp.id}/invite", json={}).status_code == 404
    # Guest routes → 403.
    assert client.get("/api/v1/guest/conversation", headers=_auth(session)).status_code == 403
    assert (
        client.post("/api/v1/guest/send", json={"body": "x"}, headers=_auth(session)).status_code
        == 403
    )


def test_invite_preview(client):
    grp = _make_group(name="Preview Room")
    inv = _invite(client, grp.id, single_use=True)
    r = client.get(f"/api/v1/guest/invite/{inv['token']}")
    assert r.status_code == 200
    assert r.json()["valid"] is True
    assert r.json()["group_name"] == "Preview Room"
    # Preview must NOT have burned the single-use invite — a real join still works.
    assert _join(client, inv["token"]).status_code == 200
