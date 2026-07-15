"""Tests for Invite Phase 0 — 1:1 DM invite as a degenerate 2-seat guest group.

Per docs/2026-07-15-sovereign-invite-join-architecture.md Phase 0 (Mode A DM):
pure reuse of the guest-group machinery. A 1:1 DM is modelled as a guest group
with exactly 2 seats and ``metadata.mode="dm"``.

Covers the acceptance:
  * ``create_dm_invite`` mints a 2-seat DM group (``metadata.mode="dm"``,
    seat 1 = operator) + a single-use invite for it.
  * ``mode=dm`` join caps at 2 seats (a third occupant → 403).
  * epoch fence: a DM guest sees NO group messages from before it joined.
  * existing group-invite behaviour unchanged; flag OFF → no new routes.
  * ``operator_create_invite`` gains ``?mode=dm|group``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    monkeypatch.setenv("SKCHAT_GUEST_OPERATOR_TOKEN", "op-secret")
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path / "skchat-home"))
    monkeypatch.setenv("SKCHAT_GUEST_REVOCATION_DB", str(tmp_path / "rev.db"))
    monkeypatch.setenv("SKCHAT_GUEST_GROUP_DB", str(tmp_path / "gg.db"))
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
    app.include_router(daemon_proxy.router)
    app.include_router(GGR.router)
    return TestClient(app)


_OP = {"X-Operator-Token": "op-secret"}
_OPERATOR = "capauth:lumina@skworld.io"


def _join(client, invite_token, name="Alice", pubkey="PUBKEY-A"):
    return client.post(
        "/api/v1/guest/join",
        json={"invite_token": invite_token, "display_name": name, "guest_pubkey": pubkey},
    )


def _auth(session):
    return {"Authorization": f"Bearer {session}"}


# ── create_dm_invite (function) ──────────────────────────────────────────────
def test_create_dm_invite_mints_2seat_dm_group(env):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)
    assert inv["mode"] == "dm"
    assert inv["single_use"] is True  # DMs default single-use
    assert inv["token"]
    assert inv["join_url"] == f"/app/#/g/{inv['token']}"

    grp = G.load_group(inv["group_id"])
    assert grp is not None
    assert grp.metadata.get("mode") == "dm"
    # Seat 1 = operator; the DM is created with exactly the operator seat filled.
    assert grp.member_count == 1
    assert grp.get_member(_OPERATOR) is not None


# ── 2-seat cap ───────────────────────────────────────────────────────────────
def test_dm_join_caps_at_two_seats(env, client):
    # Multi-use so a second, distinct guest can attempt to join the SAME dm group.
    inv = GG.create_dm_invite(operator_uri=_OPERATOR, single_use=False)

    r1 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r1.status_code == 200, r1.text
    grp = G.load_group(inv["group_id"])
    assert grp.member_count == 2  # operator + first guest

    # A second, distinct guest is the 3rd occupant → the DM is full → 403.
    r2 = _join(client, inv["token"], name="Mallory", pubkey="KEY-M")
    assert r2.status_code == 403

    # The first guest returning (same key) is idempotent — no new seat, allowed.
    r3 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r3.status_code == 200
    assert G.load_group(inv["group_id"]).member_count == 2


# ── epoch fence ──────────────────────────────────────────────────────────────
def _save_group_msg(hist, group_id, content, *, when: datetime):
    from skchat.models import ChatMessage

    hist.save(
        ChatMessage(
            sender=_OPERATOR,
            recipient=f"group:{group_id}",
            content=content,
            thread_id=group_id,
            timestamp=when,
            metadata={"group_id": group_id},
        )
    )


def test_dm_epoch_fence_hides_pre_join_history(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)
    gid = inv["group_id"]
    hist = daemon_proxy._get_history()
    # A message that pre-dates the guest joining.
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    _save_group_msg(hist, gid, "before you arrived", when=old)

    j = _join(client, inv["token"]).json()
    session = j["session_token"]
    # The join bootstrap must NOT surface the pre-join message.
    assert all(m["body"] != "before you arrived" for m in j["messages"])

    # A message the guest sends after joining IS visible; the old one stays hidden.
    client.post("/api/v1/guest/send", json={"body": "hi from guest"}, headers=_auth(session))
    conv = client.get("/api/v1/guest/conversation", headers=_auth(session)).json()
    bodies = [m["body"] for m in conv["messages"]]
    assert "hi from guest" in bodies
    assert "before you arrived" not in bodies


def test_group_guest_history_not_fenced(env, client):
    # A NON-dm group guest still sees pre-join history — behaviour unchanged.
    grp = G.create_group(name="Town Hall", creator_uri=daemon_proxy.OPERATOR_ID, members=[])
    hist = daemon_proxy._get_history()
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    _save_group_msg(hist, grp.id, "earlier group chatter", when=old)

    inv = GG.create_group_invite(grp.id)
    j = _join(client, inv["token"]).json()
    assert any(m["body"] == "earlier group chatter" for m in j["messages"])


# ── operator route ?mode=dm ──────────────────────────────────────────────────
def test_operator_route_mode_dm_creates_dm_invite(env, client):
    r = client.post("/api/v1/groups/self/invite?mode=dm", json={}, headers=_OP)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "dm"
    grp = G.load_group(body["group_id"])
    assert grp is not None
    assert grp.metadata.get("mode") == "dm"
    assert grp.member_count == 1


def test_operator_route_default_group_invite_unchanged(env, client):
    grp = G.create_group(name="Ops", creator_uri=daemon_proxy.OPERATOR_ID, members=[])
    r = client.post(f"/api/v1/groups/{grp.id}/invite", json={}, headers=_OP)
    assert r.status_code == 200, r.text
    body = r.json()
    # No new group is minted; the invite is for the SAME group, no dm mode.
    assert body["group_id"] == grp.id
    assert body["join_url"] == f"/app/#/g/{body['token']}"
    assert body.get("mode") in (None, "group")
    # Join lands in the existing group.
    assert _join(client, body["token"]).json()["group"]["id"] == grp.id


def test_flag_off_no_dm_route(env, client, monkeypatch):
    monkeypatch.setenv("SKCHAT_GUEST_LINKS_ENABLED", "0")
    r = client.post("/api/v1/groups/self/invite?mode=dm", json={}, headers=_OP)
    assert r.status_code == 404
