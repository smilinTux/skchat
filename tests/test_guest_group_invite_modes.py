"""Tests for guest-dm G2 — group invite modes (epic 8685ede6).

Depends on S1 (d964b5a7), S2 (a69e7d4e), G1 (8dc9cce0). Mirrors the 1:1 BOTH
invite decision at group scope: per-person single-use invites into a gdm
group (with the S1 alias/contact_ttl sidecar wired through), and the
already-existing shared reusable group link (classic ``create_group_invite``,
``single_use=False``) hardened with the S2 rate-limit + contact registry.
Covers:

  * a per-person single-use gdm invite carries alias/contact_ttl via the
    jti sidecar; the admitted guest lands with that alias in the
    ``dm_contacts`` registry; the alias never appears in the mint response,
    the invite JWT payload, or any guest-facing response.
  * the shared reusable link on a gdm group admits N distinct browser keys
    into ONE group (no per-arrival fanout like the DM my-DM-link), dedupes a
    returning fp (idempotent, no new seat), and rate-limits NEW admissions
    per invite jti (returning fps are never rate-limited).
  * revoking the shared-link jti blocks new joins but does not lock out
    already-admitted guests (their session tokens keep working); classic
    non-gdm group invite behaviour is unchanged.
"""

from __future__ import annotations

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


def _make_gdm_group(client):
    """Promote a fresh DM to gdm and return its group_id."""
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)
    gid = inv["group_id"]
    _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    promo = client.post(f"/api/v1/groups/{gid}/invite?mode=dm", json={}, headers=_OP).json()
    assert promo["mode"] == "gdm"
    return gid


# ── per-person single-use gdm invite: alias/contact_ttl sidecar ─────────────
def test_per_person_gdm_invite_alias_sidecar_wired_at_mint(env, client):
    gid = _make_gdm_group(client)

    r = client.post(
        f"/api/v1/groups/{gid}/invite?mode=dm",
        json={"alias": "Secret Nickname", "contact_ttl": 3600},
        headers=_OP,
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # Never echoed in the mint response.
    assert "alias" not in body
    assert "Secret Nickname" not in r.text

    # Never in the JWT payload (base64-decodable by the guest).
    import jwt as _jwt

    payload = _jwt.decode(body["token"], options={"verify_signature": False})
    assert "alias" not in payload

    # Persisted server-side, keyed by jti.
    meta = GG.get_dm_invite_meta(body["jti"])
    assert meta == {"alias": "Secret Nickname", "contact_ttl": 3600}

    joined = _join(client, body["token"], name="Bob", pubkey="KEY-B")
    assert joined.status_code == 200, joined.text
    assert "alias" not in joined.json()
    assert "Secret Nickname" not in joined.text

    # The admitted guest lands with that alias in the registry.
    fp = GG.pubkey_fingerprint("KEY-B")
    contact = GG.get_dm_contact(fp)
    assert contact is not None
    assert contact["alias"] == "Secret Nickname"
    assert contact["group_id"] == gid
    assert contact["invite_jti"] == body["jti"]
    assert contact["contact_expires_at"] is not None


def test_per_person_gdm_invite_without_alias_stores_no_sidecar_row(env, client):
    gid = _make_gdm_group(client)
    r = client.post(f"/api/v1/groups/{gid}/invite?mode=dm", json={}, headers=_OP)
    assert r.status_code == 200, r.text
    assert GG.get_dm_invite_meta(r.json()["jti"]) is None


# ── shared reusable group link: one group, dedupe, rate limit ───────────────
def test_shared_link_admits_multiple_keys_into_one_group_no_fanout(env, client):
    gid = _make_gdm_group(client)
    inv = GG.create_group_invite(gid, single_use=False)

    r_bob = _join(client, inv["token"], name="Bob", pubkey="KEY-B")
    r_carl = _join(client, inv["token"], name="Carl", pubkey="KEY-C")
    assert r_bob.status_code == 200, r_bob.text
    assert r_carl.status_code == 200, r_carl.text

    # No per-arrival fanout: both distinct guests land in the SAME group.
    assert r_bob.json()["group"]["id"] == gid
    assert r_carl.json()["group"]["id"] == gid
    grp = G.load_group(gid)
    assert grp.member_count == 4  # operator + Alice + Bob + Carl


def test_shared_link_dedupes_returning_fp(env, client):
    gid = _make_gdm_group(client)
    inv = GG.create_group_invite(gid, single_use=False)

    _join(client, inv["token"], name="Bob", pubkey="KEY-B")
    before = G.load_group(gid).member_count

    # Bob returns via the same standing link — idempotent, no new seat.
    r_again = _join(client, inv["token"], name="Bob", pubkey="KEY-B")
    assert r_again.status_code == 200, r_again.text
    assert r_again.json()["group"]["id"] == gid
    assert G.load_group(gid).member_count == before

    fp = GG.pubkey_fingerprint("KEY-B")
    contact = GG.get_dm_contact(fp)
    assert contact is not None
    assert contact["alias"] is None


def test_shared_link_rate_limits_new_admissions_per_jti(env, client, monkeypatch):
    monkeypatch.setenv("SKCHAT_DM_CONTACT_RATE_LIMIT", "2")
    gid = _make_gdm_group(client)
    inv = GG.create_group_invite(gid, single_use=False)

    r1 = _join(client, inv["token"], name="Bob", pubkey="KEY-B")
    r2 = _join(client, inv["token"], name="Carl", pubkey="KEY-C")
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text

    # Third DISTINCT key exceeds the cap of 2 new admissions for this jti.
    r3 = _join(client, inv["token"], name="Dana", pubkey="KEY-D")
    assert r3.status_code == 401
    assert r3.json() == {"detail": "invalid or expired invite"}  # generic, no oracle
    assert G.load_group(gid).member_count == 4  # operator + Alice + Bob + Carl

    # A key that ALREADY has a contact (returning) is never rate-limited.
    r4 = _join(client, inv["token"], name="Bob", pubkey="KEY-B")
    assert r4.status_code == 200, r4.text


def test_shared_link_rate_limit_does_not_apply_to_single_use_gdm_invite(env, client, monkeypatch):
    monkeypatch.setenv("SKCHAT_DM_CONTACT_RATE_LIMIT", "1")
    gid = _make_gdm_group(client)

    # Two SEPARATE per-person single-use invites: each burns after one join,
    # so the reusable-link rate cap must not reject them.
    inv1 = client.post(f"/api/v1/groups/{gid}/invite?mode=dm", json={}, headers=_OP).json()
    inv2 = client.post(f"/api/v1/groups/{gid}/invite?mode=dm", json={}, headers=_OP).json()

    r1 = _join(client, inv1["token"], name="Bob", pubkey="KEY-B")
    r2 = _join(client, inv2["token"], name="Carl", pubkey="KEY-C")
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text


# ── revoke: blocks new joins, existing guests keep working ──────────────────
def test_revoke_shared_link_blocks_new_joins_not_existing_sessions(env, client):
    gid = _make_gdm_group(client)
    inv = GG.create_group_invite(gid, single_use=False)

    r_bob = _join(client, inv["token"], name="Bob", pubkey="KEY-B")
    assert r_bob.status_code == 200, r_bob.text
    bob_session = r_bob.json()["session_token"]

    revoke = client.delete(f"/api/v1/groups/{gid}/invite/{inv['token']}", headers=_OP)
    assert revoke.status_code == 200, revoke.text

    # A brand-new guest can no longer join via the revoked link.
    r_dana = _join(client, inv["token"], name="Dana", pubkey="KEY-D")
    assert r_dana.status_code == 401

    # Bob's EXISTING session keeps working — his access is governed by
    # contact/membership status, not the (now-dead) invite jti.
    conv = client.get("/api/v1/guest/conversation", headers=_auth(bob_session))
    assert conv.status_code == 200, conv.text

    send = client.post(
        "/api/v1/guest/send", json={"body": "still here"}, headers=_auth(bob_session)
    )
    assert send.status_code == 200, send.text

    # The other (originally-admitted) guest and the group itself are untouched.
    assert G.load_group(gid).member_count == 3  # operator + Alice + Bob


# ── classic (non-gdm) group invite behaviour unchanged ───────────────────────
def test_classic_non_gdm_group_invite_unaffected_by_gdm_rate_limit(env, client, monkeypatch):
    monkeypatch.setenv("SKCHAT_DM_CONTACT_RATE_LIMIT", "1")
    grp = G.create_group(name="Town Hall", creator_uri=daemon_proxy.OPERATOR_ID, members=[])
    inv = GG.create_group_invite(grp.id, single_use=False)

    # A classic group has no dm/gdm mode metadata, so it is never rate-limited
    # regardless of how many distinct guests join the shared link.
    r1 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    r2 = _join(client, inv["token"], name="Bob", pubkey="KEY-B")
    r3 = _join(client, inv["token"], name="Carl", pubkey="KEY-C")
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r3.status_code == 200, r3.text
    assert G.load_group(grp.id).member_count == 4  # creator + Alice + Bob + Carl

    # No dm_contacts row is created for classic groups.
    for pubkey in ("KEY-A", "KEY-B", "KEY-C"):
        assert GG.get_dm_contact(GG.pubkey_fingerprint(pubkey)) is None

    # Revoking the classic invite still just stops new joins (unchanged path).
    revoke = client.delete(f"/api/v1/groups/{grp.id}/invite/{inv['token']}", headers=_OP)
    assert revoke.status_code == 200, revoke.text
    r4 = _join(client, inv["token"], name="Dana", pubkey="KEY-D")
    assert r4.status_code == 401
