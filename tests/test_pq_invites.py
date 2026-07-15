"""Unit tests for the Phase-1 signed-PQ-invite primitives (``skchat.pq_invites``).

Covers the acceptance folded in from the architecture doc's hardenings:
  * C1/C2 — operator sig verifies under the FULL inline pubkey (no directory).
  * H3    — ``bc`` commits to identity+signed-prekey ONLY; OPK rotation is a no-op.
  * H7    — fragment secret ``k`` is 32B, url-safe, and lands only in the fragment.
  * §5    — guest key binding: a valid sig verifies; a wrong bc / wrong key / a
            missing sig all fail closed (replay without the guest key → reject).
"""

from __future__ import annotations

import base64

import pytest

from skchat import pq_invites as PQI

# Matches tests/conftest.py's PASSPHRASE for the shared PGP key fixtures.
PASSPHRASE = "test-passphrase-123"


# ── Feature flag ─────────────────────────────────────────────────────────────
def test_flag_default_off(monkeypatch):
    monkeypatch.delenv(PQI.FLAG_ENV, raising=False)
    assert PQI.pq_invites_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "YES", "On"])
def test_flag_truthy(monkeypatch, val):
    monkeypatch.setenv(PQI.FLAG_ENV, val)
    assert PQI.pq_invites_enabled() is True


# ── Bundle commitment (H3) ───────────────────────────────────────────────────
def test_commitment_is_urlsafe_no_padding():
    bc = PQI.bundle_commitment("IDENTITY-KEY-ARMOR", "abcd1234hybridhex")
    assert "=" not in bc and "+" not in bc and "/" not in bc
    # 32-byte SHA-256 → 43 url-safe base64 chars (no padding).
    assert len(bc) == 43


def test_commitment_excludes_one_time_prekeys():
    """OPK rotation must NOT change bc (else it false-fails / forces OPK reuse)."""
    ik = "IDENTITY-KEY-ARMOR"
    spk = "deadbeef" * 8
    bundle_a = {"hybrid_public_hex": spk, "key_id": spk[:16], "one_time_prekeys": ["a", "b"]}
    bundle_b = {"hybrid_public_hex": spk, "key_id": spk[:16], "one_time_prekeys": ["x", "y", "z"]}
    bc = PQI.bundle_commitment(ik, spk)
    assert PQI.commitment_for_bundle(ik, bundle_a) == bc
    assert PQI.commitment_for_bundle(ik, bundle_b) == bc  # OPKs rotated → same bc


def test_commitment_sensitive_to_identity_and_signed_prekey():
    ik, spk = "IDENTITY-KEY-ARMOR", "deadbeef" * 8
    bc = PQI.bundle_commitment(ik, spk)
    assert PQI.bundle_commitment(ik + "x", spk) != bc  # tampered identity key
    assert PQI.bundle_commitment(ik, spk + "0") != bc  # tampered signed prekey


def test_verify_commitment_roundtrip_and_mismatch():
    ik, spk = "IDENTITY-KEY-ARMOR", "deadbeef" * 8
    bc = PQI.bundle_commitment(ik, spk)
    assert PQI.verify_commitment(ik, spk, bc) is True
    assert PQI.verify_commitment(ik, spk + "0", bc) is False  # bad bundle → abort
    assert PQI.verify_commitment(ik, spk, "") is False  # absent bc → fail-closed


# ── Canonical claims (order-independent) ─────────────────────────────────────
def test_canonical_claims_order_independent():
    bc = PQI.bundle_commitment("k", "s")
    a = PQI._claims_bytes(PQI.canonical_claims("agent@op.realm", bc, "dm"))
    b = PQI._claims_bytes({"mode": "dm", "idm": "agent@op.realm", "bc": bc, "junk": 1})
    assert a == b  # only the signed fields, sorted, contribute


# ── Fragment secret + join URL (H7) ──────────────────────────────────────────
def test_fragment_secret_is_32_bytes_urlsafe():
    k = PQI.new_fragment_secret()
    assert len(base64.urlsafe_b64decode(k + "=" * (-len(k) % 4))) == 32
    assert "=" not in k and len(k) == 43


def test_two_fragment_secrets_differ():
    assert PQI.new_fragment_secret() != PQI.new_fragment_secret()


def test_join_url_keeps_secrets_in_fragment():
    url = PQI.build_join_url("TOKEN.JWT.HERE", "KFRAGMENT")
    path, frag = url.split("#", 1)
    assert "?" not in path  # nothing joinable in path/query
    assert "k=" in frag and "TOKEN.JWT.HERE" in frag


def test_join_url_without_secret_is_classic():
    assert PQI.build_join_url("TOK", None) == "/app/#/g/TOK"


# ── Operator signature (C1/C2, needs pgpy) ───────────────────────────────────
@pytest.fixture
def operator_crypto(alice_keys):
    """A ChatCrypto holding the shared Alice PGP identity key (the 'operator')."""
    from skchat.crypto import ChatCrypto

    priv_armor, _pub = alice_keys
    return ChatCrypto(priv_armor, PASSPHRASE)


def test_sign_and_verify_invite_claims(operator_crypto, alice_keys):
    _priv, pub_armor = alice_keys
    claims = PQI.canonical_claims("alice@op.realm", PQI.bundle_commitment(pub_armor, "spk"), "dm")
    sig = PQI.sign_invite_claims(operator_crypto, claims)
    assert PQI.verify_invite_claims(claims, sig, pub_armor) is True


def test_forged_or_tampered_invite_rejected(operator_crypto, alice_keys, bob_keys):
    _priv, pub_armor = alice_keys
    _bpriv, bob_pub = bob_keys
    claims = PQI.canonical_claims("alice@op.realm", PQI.bundle_commitment(pub_armor, "spk"), "dm")
    sig = PQI.sign_invite_claims(operator_crypto, claims)

    # Tampered bc → signature no longer covers the claims.
    tampered = dict(claims, bc="AAAA")
    assert PQI.verify_invite_claims(tampered, sig, pub_armor) is False
    # Right claims, wrong verifying key (forged origin) → reject.
    assert PQI.verify_invite_claims(claims, sig, bob_pub) is False
    # Missing signature / missing key → fail-closed.
    assert PQI.verify_invite_claims(claims, "", pub_armor) is False
    assert PQI.verify_invite_claims(claims, sig, "") is False


# ── Guest key binding (§5, needs cryptography ECDSA P-256) ────────────────────
@pytest.fixture
def guest_keypair():
    """Generate an EC P-256 guest browser key → (spki_b64, sign(bytes)->sig_b64)."""
    ec = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ec")
    from cryptography.hazmat.primitives import hashes, serialization

    priv = ec.generate_private_key(ec.SECP256R1())
    spki = priv.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    spki_b64 = base64.b64encode(spki).decode("ascii")

    def sign(data: bytes) -> str:
        der = priv.sign(data, ec.ECDSA(hashes.SHA256()))
        return base64.b64encode(der).decode("ascii")

    return spki_b64, sign


def test_guest_binding_valid(guest_keypair):
    spki_b64, sign = guest_keypair
    jti, bc = "jti-abc", PQI.bundle_commitment("k", "s")
    sig = sign(PQI.guest_binding_bytes(jti, spki_b64, bc))
    assert PQI.verify_guest_binding(sig, spki_b64, jti, bc) is True


def test_guest_binding_wrong_bc_or_jti_fails(guest_keypair):
    spki_b64, sign = guest_keypair
    jti, bc = "jti-abc", PQI.bundle_commitment("k", "s")
    sig = sign(PQI.guest_binding_bytes(jti, spki_b64, bc))
    assert PQI.verify_guest_binding(sig, spki_b64, jti, "OTHER-BC") is False
    assert PQI.verify_guest_binding(sig, spki_b64, "other-jti", bc) is False


def test_guest_binding_wrong_key_fails(guest_keypair):
    """A replay under a DIFFERENT guest key cannot satisfy the binding."""
    ec = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ec")
    from cryptography.hazmat.primitives import serialization

    spki_b64, sign = guest_keypair
    jti, bc = "jti-abc", PQI.bundle_commitment("k", "s")
    sig = sign(PQI.guest_binding_bytes(jti, spki_b64, bc))

    other = ec.generate_private_key(ec.SECP256R1())
    other_spki = base64.b64encode(
        other.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    ).decode("ascii")
    # Same signature bytes, but claimed under a different pubkey → reject.
    assert PQI.verify_guest_binding(sig, other_spki, jti, bc) is False


def test_guest_binding_missing_sig_fails_closed():
    assert PQI.verify_guest_binding("", "PUB", "jti", "bc") is False
    assert PQI.verify_guest_binding("SIG", "", "jti", "bc") is False
    assert PQI.verify_guest_binding("SIG", "PUB", "jti", "") is False
