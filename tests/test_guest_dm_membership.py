"""Tests for guest-dm G3 - person/membership registry split + per-group revoke
+ group expiry.

Per task 2a45f549 (epic 8685ede6, depends on S2/S3/S4/G1). Covers:

  * schema evolution: ``dm_contact_memberships(fp, group_id, invite_jti,
    status, added_at)`` tracks per-group membership; migrated from legacy S2
    ``dm_contacts`` rows; a person in a DM plus a gdm has one ``dm_contacts``
    row and two ``dm_contact_memberships`` rows.
  * the S3 chokepoint now checks BOTH person status (revoked/expired, applies
    everywhere) AND membership status for the session's bound group
    (``membership_revoked``, scoped to that one group).
  * per-group revoke removes exactly that membership (roster + metadata.guests
    + access) while the group, other guests, and the person's OTHER rooms keep
    working; person-level revoke still kills every room.
  * optional gdm group expiry (``metadata.expires_at``) 403s all guest access
    to that group with reason ``group_expired``.
"""

from __future__ import annotations

import sqlite3
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


def _second_dm_family_group(client, pubkey="KEY-A", name="Alice"):
    """Promote a FRESH dm anchor to gdm and join it with the given key.

    Gives the caller a second, independent dm-family ``group_id`` (distinct
    from any plain ``create_dm_invite`` group) for the same guest fp.
    """
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)
    gid = inv["group_id"]
    promo = client.post(f"/api/v1/groups/{gid}/invite?mode=dm", json={}, headers=_OP).json()
    r = _join(client, promo["token"], name=name, pubkey=pubkey)
    assert r.status_code == 200, r.text
    return gid, r


# ── schema / migration ───────────────────────────────────────────────────────
def test_membership_table_created(env):
    conn = GG._connect()
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert "dm_contact_memberships" in tables


def test_migration_backfills_legacy_dm_contacts_row(env):
    # Simulate a pre-migration S2-only database: a dm_contacts row with no
    # corresponding membership row (as if written before this task shipped).
    conn = GG._connect()
    try:
        conn.execute(
            "INSERT INTO dm_contacts (fp, guest_id, group_id, invite_jti, alias,"
            " contact_expires_at, status, muted, created_at, last_seen_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacyfp", "guest:x#legacyfp", "legacy-group", "legacy-jti", None, None,
             "active", 0, 1000.0, 1000.0),
        )
        conn.commit()
    finally:
        conn.close()

    # Any subsequent _connect() (e.g. via a normal GG call) runs the migration.
    membership = GG.get_membership("legacyfp", "legacy-group")
    assert membership is not None
    assert membership["invite_jti"] == "legacy-jti"
    assert membership["status"] == "active"
    assert membership["added_at"] == 1000.0


def test_migration_carries_revoked_status(env):
    conn = GG._connect()
    try:
        conn.execute(
            "INSERT INTO dm_contacts (fp, guest_id, group_id, invite_jti, alias,"
            " contact_expires_at, status, muted, created_at, last_seen_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("revfp", "guest:x#revfp", "rev-group", "rev-jti", None, None,
             "revoked", 0, 500.0, 500.0),
        )
        conn.commit()
    finally:
        conn.close()

    membership = GG.get_membership("revfp", "rev-group")
    assert membership is not None
    assert membership["status"] == "revoked"


def test_migration_is_idempotent_and_does_not_clobber_live_upserts(env, client):
    # Trigger the migration path once, then do a real join through the app -
    # the migration must not stomp on rows created by normal upserts.
    GG._connect().close()
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)
    r = _join(client, inv["token"], pubkey="KEY-A")
    assert r.status_code == 200
    fp = GG.pubkey_fingerprint("KEY-A")
    gid = r.json()["group"]["id"]
    membership = GG.get_membership(fp, gid)
    assert membership is not None
    assert membership["status"] == "active"


# ── person row + multiple membership rows ────────────────────────────────────
def test_person_row_and_two_membership_rows_for_dm_plus_gdm(env, client):
    inv_dm = GG.create_dm_invite(operator_uri=_OPERATOR)
    r1 = _join(client, inv_dm["token"], pubkey="KEY-A")
    assert r1.status_code == 200, r1.text
    gid_dm = r1.json()["group"]["id"]

    gid_gdm, r2 = _second_dm_family_group(client, pubkey="KEY-A")
    assert gid_gdm != gid_dm

    fp = GG.pubkey_fingerprint("KEY-A")
    contacts = [c for c in GG.list_dm_contacts() if c["fp"] == fp]
    assert len(contacts) == 1  # exactly one PERSON row

    memberships = GG.list_memberships(fp)
    assert len(memberships) == 2  # one per group
    assert {m["group_id"] for m in memberships} == {gid_dm, gid_gdm}
    assert all(m["status"] == "active" for m in memberships)


def test_alias_and_mute_stay_person_global_across_groups(env, client):
    inv_dm = GG.create_dm_invite(operator_uri=_OPERATOR)
    GG.store_dm_invite_meta(inv_dm["jti"], alias="Bestie")
    r1 = _join(client, inv_dm["token"], pubkey="KEY-A")
    fp = GG.pubkey_fingerprint("KEY-A")
    assert GG.get_dm_contact(fp)["alias"] == "Bestie"

    GG.update_dm_contact(fp, muted=True)

    gid_gdm, _ = _second_dm_family_group(client, pubkey="KEY-A")

    # Same fp, same alias/mute after joining a SECOND, unrelated dm-family group.
    contact = GG.get_dm_contact(fp)
    assert contact["alias"] == "Bestie"
    assert contact["muted"] == 1
    # Still exactly one person row (alias/mute did not fork per group).
    assert len([c for c in GG.list_dm_contacts() if c["fp"] == fp]) == 1


# ── per-group revoke ──────────────────────────────────────────────────────────
def test_per_group_revoke_removes_only_that_membership(env, client):
    inv_dm = GG.create_dm_invite(operator_uri=_OPERATOR)
    r1 = _join(client, inv_dm["token"], pubkey="KEY-A")
    gid_dm = r1.json()["group"]["id"]
    session_dm = r1.json()["session_token"]
    guest_id = r1.json()["guest_id"]

    gid_gdm, r2 = _second_dm_family_group(client, pubkey="KEY-A")
    session_gdm = r2.json()["session_token"]

    fp = GG.pubkey_fingerprint("KEY-A")
    assert GG.revoke_group_membership(fp, gid_dm) is True

    membership = GG.get_membership(fp, gid_dm)
    assert membership["status"] == "revoked"
    other_membership = GG.get_membership(fp, gid_gdm)
    assert other_membership["status"] == "active"

    # Roster + metadata.guests: removed from gid_dm only.
    grp_dm = G.load_group(gid_dm)
    assert grp_dm.get_member(guest_id) is None
    assert guest_id not in (grp_dm.metadata.get("guests") or {})

    # The person row is untouched (not revoked).
    assert GG.get_dm_contact(fp)["status"] == "active"

    # Session bound to the revoked group is blocked...
    r_conv = client.get("/api/v1/guest/conversation", headers=_auth(session_dm))
    assert r_conv.status_code == 403
    assert r_conv.json()["detail"]["reason"] == "membership_revoked"

    # ...but the SAME person's other room keeps working.
    r_conv2 = client.get("/api/v1/guest/conversation", headers=_auth(session_gdm))
    assert r_conv2.status_code == 200, r_conv2.text


def test_per_group_revoke_leaves_group_and_other_members_untouched(env, client):
    inv_dm = GG.create_dm_invite(operator_uri=_OPERATOR)
    gid = inv_dm["group_id"]
    r1 = _join(client, inv_dm["token"], pubkey="KEY-A")
    assert r1.status_code == 200

    promo = client.post(f"/api/v1/groups/{gid}/invite?mode=dm", json={}, headers=_OP).json()
    r2 = _join(client, promo["token"], name="Bob", pubkey="KEY-B")
    assert r2.status_code == 200, r2.text
    bob_id = r2.json()["guest_id"]
    bob_session = r2.json()["session_token"]

    fp_a = GG.pubkey_fingerprint("KEY-A")
    assert GG.revoke_group_membership(fp_a, gid) is True

    grp = G.load_group(gid)
    assert grp is not None  # group itself untouched
    assert grp.get_member(bob_id) is not None  # other guest untouched

    r_bob = client.get("/api/v1/guest/conversation", headers=_auth(bob_session))
    assert r_bob.status_code == 200, r_bob.text


def test_per_group_revoke_unknown_membership_returns_false(env):
    assert GG.revoke_group_membership("no-such-fp", "no-such-group") is False


def test_per_group_revoke_route(env, client):
    inv_dm = GG.create_dm_invite(operator_uri=_OPERATOR)
    r1 = _join(client, inv_dm["token"], pubkey="KEY-A")
    gid = r1.json()["group"]["id"]
    fp = GG.pubkey_fingerprint("KEY-A")

    r = client.post(f"/api/v1/guest-dm/contacts/{fp}/groups/{gid}/revoke", headers=_OP)
    assert r.status_code == 200, r.text
    assert r.json()["revoked_fp"] == fp
    assert GG.get_membership(fp, gid)["status"] == "revoked"


def test_per_group_revoke_route_404_for_unknown(env, client):
    r = client.post(
        "/api/v1/guest-dm/contacts/nope/groups/no-group/revoke", headers=_OP
    )
    assert r.status_code == 404


def test_per_group_revoke_route_is_dataplane_gated():
    from skchat.dataplane_paths import is_gated

    assert is_gated("POST", "/api/v1/guest-dm/contacts/abc/groups/def/revoke") is True


def test_per_group_revoke_route_requires_operator(env, client):
    inv_dm = GG.create_dm_invite(operator_uri=_OPERATOR)
    r1 = _join(client, inv_dm["token"], pubkey="KEY-A")
    gid = r1.json()["group"]["id"]
    fp = GG.pubkey_fingerprint("KEY-A")

    r = client.post(f"/api/v1/guest-dm/contacts/{fp}/groups/{gid}/revoke")
    assert r.status_code == 401


def test_per_group_revoke_blocks_reentry_for_that_group_only(env, client):
    inv_dm = GG.create_dm_invite(operator_uri=_OPERATOR)  # single-use
    r1 = _join(client, inv_dm["token"], pubkey="KEY-A")
    gid = r1.json()["group"]["id"]
    fp = GG.pubkey_fingerprint("KEY-A")

    assert GG.revoke_group_membership(fp, gid) is True

    r2 = _join(client, inv_dm["token"], pubkey="KEY-A")
    assert r2.status_code == 401
    assert r2.json() == {"detail": "invalid or expired invite"}


# ── person-level revoke still kills everything ───────────────────────────────
def test_person_level_revoke_still_kills_all_rooms(env, client):
    inv_dm = GG.create_dm_invite(operator_uri=_OPERATOR)
    r1 = _join(client, inv_dm["token"], pubkey="KEY-A")
    session_dm = r1.json()["session_token"]

    gid_gdm, r2 = _second_dm_family_group(client, pubkey="KEY-A")
    session_gdm = r2.json()["session_token"]

    fp = GG.pubkey_fingerprint("KEY-A")
    assert GG.revoke_dm_contact(fp) is True

    for session in (session_dm, session_gdm):
        r = client.get("/api/v1/guest/conversation", headers=_auth(session))
        assert r.status_code == 403
        assert r.json()["detail"]["reason"] == "contact_revoked"


# ── group expiry ──────────────────────────────────────────────────────────────
def test_expired_gdm_group_403s_guest_access(env, client):
    inv_dm = GG.create_dm_invite(operator_uri=_OPERATOR)
    gid = inv_dm["group_id"]
    r1 = _join(client, inv_dm["token"], pubkey="KEY-A")
    session = r1.json()["session_token"]

    grp = G.load_group(gid)
    grp.metadata["expires_at"] = time.time() - 10
    G.save_group(grp)

    r = client.get("/api/v1/guest/conversation", headers=_auth(session))
    assert r.status_code == 403
    assert r.json()["detail"]["reason"] == "group_expired"


def test_unexpired_gdm_group_unaffected(env, client):
    inv_dm = GG.create_dm_invite(operator_uri=_OPERATOR)
    gid = inv_dm["group_id"]
    r1 = _join(client, inv_dm["token"], pubkey="KEY-A")
    session = r1.json()["session_token"]

    grp = G.load_group(gid)
    grp.metadata["expires_at"] = time.time() + 3600
    G.save_group(grp)

    r = client.get("/api/v1/guest/conversation", headers=_auth(session))
    assert r.status_code == 200


def test_group_expiry_only_affects_that_group(env, client):
    inv_dm = GG.create_dm_invite(operator_uri=_OPERATOR)
    gid = inv_dm["group_id"]
    r1 = _join(client, inv_dm["token"], pubkey="KEY-A")
    session_expired = r1.json()["session_token"]

    gid_gdm, r2 = _second_dm_family_group(client, pubkey="KEY-A")
    session_ok = r2.json()["session_token"]

    grp = G.load_group(gid)
    grp.metadata["expires_at"] = time.time() - 10
    G.save_group(grp)

    r_expired = client.get("/api/v1/guest/conversation", headers=_auth(session_expired))
    assert r_expired.status_code == 403
    assert r_expired.json()["detail"]["reason"] == "group_expired"

    r_ok = client.get("/api/v1/guest/conversation", headers=_auth(session_ok))
    assert r_ok.status_code == 200


def test_expiry_does_not_affect_classic_non_dm_groups(env, client):
    grp = G.create_group(name="Ops", creator_uri=daemon_proxy.OPERATOR_ID, members=[])
    inv = GG.create_group_invite(grp.id)
    r = _join(client, inv["token"], pubkey="KEY-A")
    assert r.status_code == 200
    session = r.json()["session_token"]

    grp2 = G.load_group(grp.id)
    grp2.metadata["expires_at"] = time.time() - 10
    G.save_group(grp2)

    r2 = client.get("/api/v1/guest/conversation", headers=_auth(session))
    # No dm_contacts/membership row exists for a classic guest, and expiry is
    # only enforced for dm-family (dm/gdm) groups.
    assert r2.status_code == 200


# ── reason codes are distinguishable ─────────────────────────────────────────
def test_reason_codes_are_distinct(env, client):
    # membership_revoked
    inv1 = GG.create_dm_invite(operator_uri=_OPERATOR)
    gid1 = inv1["group_id"]
    r1 = _join(client, inv1["token"], pubkey="KEY-M")
    fp1 = GG.pubkey_fingerprint("KEY-M")
    GG.revoke_group_membership(fp1, gid1)
    resp1 = client.get(
        "/api/v1/guest/conversation", headers=_auth(r1.json()["session_token"])
    )

    # contact_revoked
    inv2 = GG.create_dm_invite(operator_uri=_OPERATOR)
    r2 = _join(client, inv2["token"], pubkey="KEY-P")
    fp2 = GG.pubkey_fingerprint("KEY-P")
    GG.revoke_dm_contact(fp2)
    resp2 = client.get(
        "/api/v1/guest/conversation", headers=_auth(r2.json()["session_token"])
    )

    # group_expired
    inv3 = GG.create_dm_invite(operator_uri=_OPERATOR)
    gid3 = inv3["group_id"]
    r3 = _join(client, inv3["token"], pubkey="KEY-E")
    grp3 = G.load_group(gid3)
    grp3.metadata["expires_at"] = time.time() - 10
    G.save_group(grp3)
    resp3 = client.get(
        "/api/v1/guest/conversation", headers=_auth(r3.json()["session_token"])
    )

    reasons = {
        resp1.json()["detail"]["reason"],
        resp2.json()["detail"]["reason"],
        resp3.json()["detail"]["reason"],
    }
    assert reasons == {"membership_revoked", "contact_revoked", "group_expired"}


# ── unit-level GG helper coverage ────────────────────────────────────────────
def test_get_membership_none_for_unknown(env):
    assert GG.get_membership("nope", "nope") is None


def test_find_guest_id_by_fp(env, client):
    inv_dm = GG.create_dm_invite(operator_uri=_OPERATOR)
    r1 = _join(client, inv_dm["token"], pubkey="KEY-A")
    gid = r1.json()["group"]["id"]
    fp = GG.pubkey_fingerprint("KEY-A")
    grp = G.load_group(gid)
    assert GG.find_guest_id_by_fp(grp, fp) == r1.json()["guest_id"]
    assert GG.find_guest_id_by_fp(grp, "no-such-fp") is None
