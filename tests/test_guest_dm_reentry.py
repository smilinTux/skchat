"""Tests for guest-dm S3 - burned single-use re-entry + revoke/expiry chokepoint.

Per task a0b8f930 (epic 8685ede6, depends on S2 a69e7d4e). Covers:

  * a single-use dm invite whose jti is already burned still lets the SAME
    previously-admitted guest key back in: fresh session, no new seat, no new
    group. Any other presenter (different key, stranger) gets the existing
    generic 401 - no oracle.
  * the S3 enforcement chokepoint: a revoked ``dm_contacts`` row 403s every
    guest route with ``{"reason": "contact_revoked"}`` and blocks re-entry; an
    expired contact 403s with ``{"reason": "contact_expired"}``.
  * the Phase-1 guest-binding check still gates the re-entry path when PQ
    invites are on.
"""

from __future__ import annotations

import base64

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


_OPERATOR = "capauth:lumina@skworld.io"


def _join(client, invite_token, name="Alice", pubkey="PUBKEY-A"):
    return client.post(
        "/api/v1/guest/join",
        json={"invite_token": invite_token, "display_name": name, "guest_pubkey": pubkey},
    )


def _auth(session):
    return {"Authorization": f"Bearer {session}"}


# ── re-entry: same key on a burned single-use jti ────────────────────────────
def test_reentry_same_key_burned_invite_gets_fresh_session(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)  # single-use by default
    r1 = _join(client, inv["token"], pubkey="KEY-A")
    assert r1.status_code == 200, r1.text
    gid = r1.json()["group"]["id"]
    session_1 = r1.json()["session_token"]
    assert G.load_group(gid).member_count == 2

    # The invite's single-use jti is already burned; the SAME browser key
    # presents it again (e.g. its 7d session JWT expired) - re-admitted.
    r2 = _join(client, inv["token"], pubkey="KEY-A")
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["group"]["id"] == gid
    assert body2["session_token"] != session_1  # a FRESH token, not a replay
    assert body2["guest_id"] == r1.json()["guest_id"]

    # No new member, no new group.
    grp = G.load_group(gid)
    assert grp.member_count == 2
    assert grp.metadata.get("mode") == "dm"

    # The fresh session actually works.
    r3 = client.post(
        "/api/v1/guest/send", json={"body": "back again"}, headers=_auth(body2["session_token"])
    )
    assert r3.status_code == 200, r3.text


def test_reentry_ignores_display_name_change_no_mutation(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)
    r1 = _join(client, inv["token"], name="Alice", pubkey="KEY-A")
    gid = r1.json()["group"]["id"]

    r2 = _join(client, inv["token"], name="TotallyDifferentName", pubkey="KEY-A")
    assert r2.status_code == 200, r2.text
    # Re-entry never mutates the group - the roster display name is unchanged.
    assert r2.json()["display_name"] == "Alice"
    member = G.load_group(gid).get_member(r1.json()["guest_id"])
    assert member.display_name == "Alice"


# ── no oracle: a different key / a stranger on a burned jti ──────────────────
def test_reentry_rejects_different_key_on_burned_jti(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)
    r1 = _join(client, inv["token"], pubkey="KEY-A")
    assert r1.status_code == 200
    gid = r1.json()["group"]["id"]

    # A DIFFERENT browser key replays the same (now-burned) invite token.
    r2 = _join(client, inv["token"], name="Mallory", pubkey="KEY-M")
    assert r2.status_code == 401
    assert r2.json() == {"detail": "invalid or expired invite"}

    # No side effects: no new contact, no new member, group untouched.
    assert GG.get_dm_contact(GG.pubkey_fingerprint("KEY-M")) is None
    assert G.load_group(gid).member_count == 2


def test_reentry_stranger_no_oracle_same_generic_error(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)
    _join(client, inv["token"], pubkey="KEY-A")  # burns the invite

    # A never-before-seen key AND a genuinely bogus token both fail with the
    # exact same generic body - no distinguishing signal for strangers.
    r_used = _join(client, inv["token"], name="Stranger", pubkey="KEY-NEVER-SEEN")
    r_bogus = client.post(
        "/api/v1/guest/join",
        json={"invite_token": "not-a-real-token", "display_name": "X", "guest_pubkey": "Y"},
    )
    assert r_used.status_code == r_bogus.status_code == 401
    assert r_used.json() == r_bogus.json() == {"detail": "invalid or expired invite"}


def test_reentry_only_applies_to_dm_mode_groups(env, client):
    """A classic (non-dm) single-use group invite double-join is unchanged -
    no re-entry semantics leak into ordinary guest-group joins."""
    grp = G.create_group(name="Town Hall", creator_uri=daemon_proxy.OPERATOR_ID, members=[])
    inv = GG.create_group_invite(grp.id, single_use=True)
    r1 = _join(client, inv["token"], pubkey="KEY-A")
    assert r1.status_code == 200

    r2 = _join(client, inv["token"], pubkey="KEY-A")  # same key, same burned jti
    assert r2.status_code == 401
    assert r2.json() == {"detail": "invalid or expired invite"}


# ── enforcement chokepoint: revoked contact ───────────────────────────────────
def test_revoked_contact_403s_every_guest_route(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)
    r1 = _join(client, inv["token"], pubkey="KEY-A")
    session = r1.json()["session_token"]
    fp = GG.pubkey_fingerprint("KEY-A")

    assert GG.revoke_dm_contact(fp) is True
    assert GG.get_dm_contact(fp)["status"] == "revoked"

    r_conv = client.get("/api/v1/guest/conversation", headers=_auth(session))
    assert r_conv.status_code == 403
    assert r_conv.json()["detail"]["reason"] == "contact_revoked"

    r_send = client.post(
        "/api/v1/guest/send", json={"body": "hi"}, headers=_auth(session)
    )
    assert r_send.status_code == 403
    assert r_send.json()["detail"]["reason"] == "contact_revoked"

    r_call = client.post("/api/v1/guest/call", json={}, headers=_auth(session))
    assert r_call.status_code == 403
    assert r_call.json()["detail"]["reason"] == "contact_revoked"


def test_revoke_dm_contact_kills_the_invite_link_too(env, client):
    from skchat import guest as _guest

    inv = GG.create_dm_invite(operator_uri=_OPERATOR)
    _join(client, inv["token"], pubkey="KEY-A")
    fp = GG.pubkey_fingerprint("KEY-A")

    GG.revoke_dm_contact(fp)
    assert _guest._is_revoked(inv["jti"]) is True


def test_revoked_contact_blocks_reentry(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)
    r1 = _join(client, inv["token"], pubkey="KEY-A")
    assert r1.status_code == 200
    fp = GG.pubkey_fingerprint("KEY-A")
    GG.revoke_dm_contact(fp)

    r2 = _join(client, inv["token"], pubkey="KEY-A")
    assert r2.status_code == 401  # re-entry blocked; same generic error, no oracle
    assert r2.json() == {"detail": "invalid or expired invite"}


def test_revoke_dm_contact_noop_for_unknown_fp(env):
    assert GG.revoke_dm_contact("no-such-fp") is False


# ── enforcement chokepoint: expired contact ───────────────────────────────────
def test_expired_contact_403s_with_reason(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)
    GG.store_dm_invite_meta(inv["jti"], contact_ttl=-10)  # already expired at admission
    r1 = _join(client, inv["token"], pubkey="KEY-A")
    assert r1.status_code == 200
    session = r1.json()["session_token"]

    r2 = client.get("/api/v1/guest/conversation", headers=_auth(session))
    assert r2.status_code == 403
    assert r2.json()["detail"]["reason"] == "contact_expired"


def test_expired_contact_blocks_reentry(env, client):
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)
    GG.store_dm_invite_meta(inv["jti"], contact_ttl=-10)
    r1 = _join(client, inv["token"], pubkey="KEY-A")
    assert r1.status_code == 200

    r2 = _join(client, inv["token"], pubkey="KEY-A")
    assert r2.status_code == 401
    assert r2.json() == {"detail": "invalid or expired invite"}


# ── unaffected: classic non-dm guests never touch dm_contacts ────────────────
def test_classic_group_guest_unaffected_by_chokepoint(env, client):
    grp = G.create_group(name="Ops", creator_uri=daemon_proxy.OPERATOR_ID, members=[])
    inv = GG.create_group_invite(grp.id)
    r = _join(client, inv["token"], pubkey="KEY-A")
    assert r.status_code == 200
    session = r.json()["session_token"]
    r2 = client.get("/api/v1/guest/conversation", headers=_auth(session))
    assert r2.status_code == 200


# ── Phase 1 guest-binding still gates re-entry when PQ is on ────────────────
@pytest.fixture
def operator(alice_keys):
    from skchat.crypto import ChatCrypto

    priv, pub = alice_keys
    return ChatCrypto(priv, "test-passphrase-123"), pub


@pytest.fixture
def pq_env(env, monkeypatch, operator):
    from skchat import pq_invites as PQI

    monkeypatch.setenv("SKCHAT_PQ_INVITES_ENABLED", "1")
    crypto, pub = operator
    signed_prekey = "deadbeef" * 8

    def _fake_material(mode):
        bc = PQI.bundle_commitment(pub, signed_prekey)
        claims = PQI.canonical_claims("alice@op.realm", bc, mode)
        return {
            "idm": "alice@op.realm",
            "operator_pubkey": pub,
            "ik_fp": crypto.fingerprint,
            "signed_prekey": signed_prekey,
            "bc": bc,
            "mode": mode,
            "operator_sig": PQI.sign_invite_claims(crypto, claims),
        }

    monkeypatch.setattr(PQI, "resolve_operator_material", _fake_material)
    return env


@pytest.fixture
def guest_keypair():
    ec = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ec")
    from cryptography.hazmat.primitives import hashes, serialization

    priv = ec.generate_private_key(ec.SECP256R1())
    spki_b64 = base64.b64encode(
        priv.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    ).decode("ascii")

    def sign(data: bytes) -> str:
        return base64.b64encode(priv.sign(data, ec.ECDSA(hashes.SHA256()))).decode("ascii")

    return spki_b64, sign


def test_reentry_requires_valid_guest_binding_when_pq_on(pq_env, client, guest_keypair):
    from skchat import pq_invites as PQI

    spki_b64, sign = guest_keypair
    inv = GG.create_dm_invite(operator_uri=_OPERATOR)

    sig = sign(PQI.guest_binding_bytes(inv["jti"], spki_b64, inv["bc"]))
    r1 = client.post(
        "/api/v1/guest/join",
        json={
            "invite_token": inv["token"],
            "display_name": "Alice",
            "guest_pubkey": spki_b64,
            "guest_sig": sig,
        },
    )
    assert r1.status_code == 200, r1.text

    # Re-entry WITHOUT a valid binding (missing sig) is rejected even though
    # the fp/contact/membership would otherwise qualify.
    r2 = client.post(
        "/api/v1/guest/join",
        json={
            "invite_token": inv["token"],
            "display_name": "Alice",
            "guest_pubkey": spki_b64,
        },
    )
    assert r2.status_code == 401

    # Re-entry WITH a valid binding succeeds.
    sig2 = sign(PQI.guest_binding_bytes(inv["jti"], spki_b64, inv["bc"]))
    r3 = client.post(
        "/api/v1/guest/join",
        json={
            "invite_token": inv["token"],
            "display_name": "Alice",
            "guest_pubkey": spki_b64,
            "guest_sig": sig2,
        },
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["session_token"] != r1.json()["session_token"]
