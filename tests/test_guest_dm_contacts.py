"""Tests for guest-dm S2 — dm_contacts registry + reusable-link per-guest DM
admission (epic 8685ede6, depends on S1 d964b5a7).

Covers the acceptance:
  * reusable dm invite: two different browser keys land in two SEPARATE 2-seat
    dm groups; the same key returns to its own group + history.
  * every dm admission upserts dm_contacts (fp, group_id, jti, alias from the
    invite's sidecar, contact expiry); last_seen_at updates on rejoin.
  * new-contact rate limit per reusable invite jti, generic 401 past the cap;
    single-use dm flow and classic group joins are unaffected.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy
from skchat import daemon_proxy_groups as G
from skchat import guest_dm as DM
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


_OPERATOR = "capauth:lumina@skworld.io"


def _join(client, invite_token, name="Alice", pubkey="PUBKEY-A"):
    return client.post(
        "/api/v1/guest/join",
        json={"invite_token": invite_token, "display_name": name, "guest_pubkey": pubkey},
    )


def _auth(session):
    return {"Authorization": f"Bearer {session}"}


# ── reusable-link fanout ─────────────────────────────────────────────────────
def test_reusable_dm_invite_fans_distinct_guests_into_separate_groups(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR, single_use=False)
    anchor_gid = inv["group_id"]

    r1 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r1.status_code == 200, r1.text
    j1 = r1.json()
    assert j1["group"]["id"] == anchor_gid  # first arrival fills the anchor group
    assert G.load_group(anchor_gid).member_count == 2

    # A second, distinct guest does NOT collide with Alice — it gets its OWN
    # fresh 2-seat dm group instead of a 403.
    r2 = _join(client, inv["token"], name="Mallory", pubkey="KEY-M")
    assert r2.status_code == 200, r2.text
    j2 = r2.json()
    mallory_gid = j2["group"]["id"]
    assert mallory_gid != anchor_gid
    fresh = G.load_group(mallory_gid)
    assert fresh is not None
    assert fresh.metadata.get("mode") == "dm"
    assert fresh.member_count == 2  # operator + Mallory only

    # Alice is NOT a member of Mallory's group, and vice versa.
    assert fresh.get_member(j1["guest_id"]) is None
    assert G.load_group(anchor_gid).get_member(j2["guest_id"]) is None


def test_reusable_dm_invite_same_key_returns_to_own_group_and_history(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR, single_use=False)

    r1 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    gid = r1.json()["group"]["id"]
    session = r1.json()["session_token"]
    client.post("/api/v1/guest/send", json={"body": "hi it's alice"}, headers=_auth(session))

    # A different guest fans out into a new group first.
    _join(client, inv["token"], name="Mallory", pubkey="KEY-M")

    # Alice returns (same browser key) — lands back in HER group, with history.
    r3 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r3.status_code == 200, r3.text
    j3 = r3.json()
    assert j3["group"]["id"] == gid
    assert any(m["body"] == "hi it's alice" for m in j3["messages"])
    assert G.load_group(gid).member_count == 2  # no duplicate seat


# ── dm_contacts registry ──────────────────────────────────────────────────────
def test_dm_admission_upserts_dm_contacts(env, client):
    inv = GG.create_dm_invite(
        operator_uri=_OPERATOR, single_use=False, alias="Front Desk", contact_ttl=3600
    )
    r1 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    j1 = r1.json()
    fp = j1["fingerprint"]

    contact = DM.get_contact(fp)
    assert contact is not None
    assert contact["group_id"] == j1["group"]["id"]
    assert contact["invite_jti"] == inv["jti"]
    assert contact["alias"] == "Front Desk"
    assert contact["contact_expires_at"] is not None
    assert contact["contact_expires_at"] > contact["created_at"]
    first_seen = contact["last_seen_at"]

    # Alias must never leak into the join response or any /guest/* payload.
    assert "alias" not in j1
    assert "Front Desk" not in r1.text

    # Rejoin (same key) refreshes last_seen_at without minting a new contact/group.
    r2 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r2.status_code == 200
    contact2 = DM.get_contact(fp)
    assert contact2["group_id"] == contact["group_id"]
    assert contact2["last_seen_at"] >= first_seen


def test_single_use_dm_admission_also_upserts_contact(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR, single_use=True)
    r1 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r1.status_code == 200
    fp = r1.json()["fingerprint"]
    contact = DM.get_contact(fp)
    assert contact is not None
    assert contact["group_id"] == r1.json()["group"]["id"]
    assert contact["invite_jti"] == inv["jti"]


def test_classic_group_join_does_not_touch_dm_contacts(env, client):
    grp = G.create_group(name="Town Hall", creator_uri=daemon_proxy.OPERATOR_ID, members=[])
    inv = GG.create_group_invite(grp.id)
    r = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r.status_code == 200
    fp = r.json()["fingerprint"]
    assert DM.get_contact(fp) is None


# ── new-contact rate limit ────────────────────────────────────────────────────
def test_new_contact_rate_limit_returns_generic_401(env, client, monkeypatch):
    monkeypatch.setenv("SKCHAT_DM_CONTACT_RATE_LIMIT", "1")
    inv = GG.create_dm_invite(operator_uri=_OPERATOR, single_use=False)

    r1 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r1.status_code == 200  # first arrival fills the anchor, no rate check yet

    # Second distinct guest is the first FRESH-contact fanout — consumes the
    # rate budget of 1.
    r2 = _join(client, inv["token"], name="Mallory", pubkey="KEY-M")
    assert r2.status_code == 200

    # Third distinct guest would be a second fresh contact — over the cap.
    r3 = _join(client, inv["token"], name="Bob", pubkey="KEY-B")
    assert r3.status_code == 401
    assert r3.json() == {"detail": "invalid or expired invite"}  # generic, no oracle

    # A RETURNING guest (already-known fp) is never rate-limited.
    r4 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r4.status_code == 200


def test_single_use_dm_flow_unchanged_by_rate_limit(env, client, monkeypatch):
    monkeypatch.setenv("SKCHAT_DM_CONTACT_RATE_LIMIT", "1")
    inv = GG.create_dm_invite(operator_uri=_OPERATOR, single_use=True)
    r1 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    assert r1.status_code == 200
