"""Phase-1 signed-invite flow tests (routes end-to-end, ``SKCHAT_PQ_INVITES_ENABLED``).

Exercises the acceptance from task 54c612f6 against the real FastAPI routes:
  * invite carries full operator pubkey + FQID idm + operator sig (no directory).
  * bc commits to identity+signed-prekey only (OPK rotation covered in the unit
    suite); preview surfaces {idm, full_pubkey, bc, mode, operator_sig}.
  * bad bc / forged sig → the joiner's verify fails (fail-closed abort).
  * fragment k present in join_url; nothing joinable in path/query.
  * guest_join: replay without a valid guest key → 401; valid binding → join.

The operator crypto material is injected (``resolve_operator_material``
monkeypatched onto a real test PGP key) so the operator signature is genuine and
verifiable, without needing a live capauth identity or liboqs backend.
"""

from __future__ import annotations

import base64

import jwt as _jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy
from skchat import daemon_proxy_groups as G
from skchat import guest_group_routes as GGR
from skchat import guest_groups as GG
from skchat import pq_invites as PQI

PASSPHRASE = "test-passphrase-123"  # matches tests/conftest.py key fixtures
_SECRET = "x" * 48
_SIGNED_PREKEY = "deadbeef" * 8  # stand-in operator hybrid_public_hex
_OP = {"X-Operator-Token": "op-secret"}


@pytest.fixture
def operator(alice_keys):
    from skchat.crypto import ChatCrypto

    priv, pub = alice_keys
    return ChatCrypto(priv, PASSPHRASE), pub


@pytest.fixture
def env(tmp_path, monkeypatch, operator):
    monkeypatch.setenv("SKCHAT_GUEST_TOKEN_SECRET", _SECRET)
    monkeypatch.setenv("SKCHAT_GUEST_LINKS_ENABLED", "1")
    monkeypatch.setenv("SKCHAT_PQ_INVITES_ENABLED", "1")
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

    # Inject a genuine operator signature over the canonical claims.
    crypto, pub = operator

    def _fake_material(mode):
        bc = PQI.bundle_commitment(pub, _SIGNED_PREKEY)
        claims = PQI.canonical_claims("alice@op.realm", bc, mode)
        return {
            "idm": "alice@op.realm",
            "operator_pubkey": pub,
            "ik_fp": crypto.fingerprint,
            "signed_prekey": _SIGNED_PREKEY,
            "bc": bc,
            "mode": mode,
            "operator_sig": PQI.sign_invite_claims(crypto, claims),
        }

    monkeypatch.setattr(PQI, "resolve_operator_material", _fake_material)
    return tmp_path


@pytest.fixture
def client(env):
    app = FastAPI()
    app.include_router(daemon_proxy.router)
    app.include_router(GGR.router)
    return TestClient(app)


@pytest.fixture
def guest_keypair():
    """EC P-256 guest browser key → (spki_b64, sign(bytes)->sig_b64)."""
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


def _make_group(name="Town Hall", members=("lumina",)):
    return G.create_group(name=name, creator_uri=daemon_proxy.OPERATOR_ID, members=list(members))


def _invite(client, group_id, **kw):
    r = client.post(f"/api/v1/groups/{group_id}/invite", json=kw, headers=_OP)
    assert r.status_code == 200, r.text
    return r.json()


# ── invite carries operator material + fragment secret ───────────────────────
def test_invite_carries_full_pubkey_fqid_sig_and_fragment(client):
    grp = _make_group()
    inv = _invite(client, grp.id)

    # Fragment secret present; nothing joinable in path/query (H7).
    assert inv["join_url"].startswith(f"/app/#/g/{inv['token']}&k=")
    path = inv["join_url"].split("#", 1)[0]
    assert "?" not in path and "k=" not in path
    frag_k = inv["join_url"].split("&k=", 1)[1]
    assert len(base64.urlsafe_b64decode(frag_k + "=" * (-len(frag_k) % 4))) == 32

    # Token carries the operator-signed identity claims + FULL inline pubkey.
    payload = _jwt.decode(inv["token"], _SECRET, algorithms=["HS256"])
    assert payload["idm"] == "alice@op.realm"  # full FQID (C1)
    assert payload["mode"] == "group"
    assert payload["bc"] == inv["bc"]
    assert payload["op_pub"].startswith("-----BEGIN PGP PUBLIC KEY")  # full key inline (C2)

    # Self-authenticating: the operator sig verifies under the inline pubkey,
    # no directory lookup needed.
    claims = PQI.canonical_claims(payload["idm"], payload["bc"], payload["mode"])
    assert PQI.verify_invite_claims(claims, payload["op_sig"], payload["op_pub"]) is True


def test_preview_returns_material_and_commitment_holds(client):
    grp = _make_group()
    inv = _invite(client, grp.id)

    r = client.get(f"/api/v1/guest/invite/{inv['token']}")
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["valid"] is True
    assert p["idm"] == "alice@op.realm"
    assert p["full_pubkey"].startswith("-----BEGIN PGP PUBLIC KEY")

    # Joiner verifies operator sig + bundle commitment BEFORE any handshake.
    claims = PQI.canonical_claims(p["idm"], p["bc"], p["mode"])
    assert PQI.verify_invite_claims(claims, p["operator_sig"], p["full_pubkey"]) is True
    assert PQI.verify_commitment(p["full_pubkey"], _SIGNED_PREKEY, p["bc"]) is True


def test_forged_bc_or_sig_aborts(client):
    """A tampered bc (or any forged claim) fails the joiner's verify → abort."""
    grp = _make_group()
    inv = _invite(client, grp.id)

    # Re-mint a token with a tampered bc (we hold the server secret in the test).
    payload = _jwt.decode(inv["token"], _SECRET, algorithms=["HS256"])
    payload["bc"] = "TAMPERED-BC"
    forged = _jwt.encode(payload, _SECRET, algorithm="HS256")

    p = client.get(f"/api/v1/guest/invite/{forged}").json()
    claims = PQI.canonical_claims(p["idm"], p["bc"], p["mode"])
    # Operator sig no longer covers the tampered claims → fail-closed (no silent
    # classical fallback).
    assert PQI.verify_invite_claims(claims, p["operator_sig"], p["full_pubkey"]) is False
    # And the commitment against the real signed prekey no longer matches.
    assert PQI.verify_commitment(p["full_pubkey"], _SIGNED_PREKEY, p["bc"]) is False


# ── guest_join key binding (§5) ──────────────────────────────────────────────
def test_join_replay_without_guest_key_is_401(client):
    grp = _make_group()
    inv = _invite(client, grp.id)

    # Attacker holds the link but not a guest key → no valid binding → 401.
    r = client.post(
        "/api/v1/guest/join",
        json={"invite_token": inv["token"], "display_name": "Mallory", "guest_pubkey": ""},
    )
    assert r.status_code == 401

    # A garbage pubkey/sig also fails closed.
    r2 = client.post(
        "/api/v1/guest/join",
        json={
            "invite_token": inv["token"],
            "display_name": "Mallory",
            "guest_pubkey": "AAAA",
            "guest_sig": "BBBB",
        },
    )
    assert r2.status_code == 401


def test_join_single_use_not_burned_on_binding_failure(client):
    """A failed binding must not consume a single-use invite (peek-before-burn)."""
    grp = _make_group()
    inv = _invite(client, grp.id, single_use=True)

    bad = client.post(
        "/api/v1/guest/join",
        json={"invite_token": inv["token"], "display_name": "Mallory", "guest_pubkey": "AAAA"},
    )
    assert bad.status_code == 401
    # The invite is still live — a second, still-unbound attempt is the SAME 401
    # (would be a distinct "already used" path if the first had burned it).
    again = client.post(
        "/api/v1/guest/join",
        json={"invite_token": inv["token"], "display_name": "Mallory2", "guest_pubkey": "CCCC"},
    )
    assert again.status_code == 401


def test_join_with_valid_binding_succeeds(client, guest_keypair):
    spki_b64, sign = guest_keypair
    grp = _make_group()
    inv = _invite(client, grp.id)

    sig = sign(PQI.guest_binding_bytes(inv["jti"], spki_b64, inv["bc"]))
    r = client.post(
        "/api/v1/guest/join",
        json={
            "invite_token": inv["token"],
            "display_name": "Alice",
            "guest_pubkey": spki_b64,
            "guest_sig": sig,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trust"] == "untrusted"
    assert body["group"]["id"] == grp.id


# ── flag off → classic invite unchanged ──────────────────────────────────────
def test_pq_flag_off_invite_is_classic(monkeypatch):
    monkeypatch.setenv("SKCHAT_GUEST_TOKEN_SECRET", _SECRET)
    monkeypatch.delenv("SKCHAT_PQ_INVITES_ENABLED", raising=False)
    inv = GG.create_group_invite("gid-123")
    assert inv["join_url"] == f"/app/#/g/{inv['token']}"
    for k in ("bc", "idm", "fragment_secret"):
        assert k not in inv
    payload = _jwt.decode(inv["token"], _SECRET, algorithms=["HS256"])
    for k in ("idm", "bc", "mode", "op_sig", "op_pub"):
        assert k not in payload
