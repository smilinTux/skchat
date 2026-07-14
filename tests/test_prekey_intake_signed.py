"""P0.5 SEAM 7 — fail-closed signed-prekey intake on the app path.

The app publishes its device prekey bundle via ``POST /api/v1/prekey``; the
daemon persists it (``pq_prekeys.store_app_prekey_bundle``). Historically that
intake accepted an unsigned (``signature: null``) bundle and verified nothing,
so a handshake MITM could substitute its own hybrid public key.

These tests prove the GATED fail-closed behaviour: when
``SKCHAT_REQUIRE_SIGNED_PREKEYS`` is set, only a bundle carrying a signature that
verifies under the claimed identity's key is stored; unsigned/invalid bundles are
rejected (nothing stored). When the flag is unset the path is UNCHANGED.

Signature verification is the pure ``skchat.prekey_sig`` helper (PGP only — no
liboqs needed), so these tests run without a PQ backend.
"""

from __future__ import annotations

import importlib

import pytest

from skchat.crypto import ChatCrypto
from skchat.prekey_sig import sign_prekey_bundle

PASSPHRASE = "test-passphrase-123"


@pytest.fixture()
def PQ(tmp_path, monkeypatch):
    """pq_prekeys bound to an isolated SKCHAT_HOME (fresh peer store)."""
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    from skchat import pq_prekeys

    importlib.reload(pq_prekeys)
    return pq_prekeys


@pytest.fixture()
def alice_crypto(alice_keys: tuple[str, str]) -> ChatCrypto:
    private, _ = alice_keys
    return ChatCrypto(private, PASSPHRASE)


@pytest.fixture()
def unsigned_bundle() -> dict:
    """A hybrid prekey bundle in the app-published shape (signature: null)."""
    pub_hex = "ab" * 32
    return {
        "suite": "x25519-mlkem768",
        "hybrid_public_hex": pub_hex,
        "signature": None,
        "key_id": pub_hex[:16],
        "device_id": "chef-web",
        "ratchet": "pqdr1",
    }


def _signed(alice_crypto: ChatCrypto, unsigned_bundle: dict) -> dict:
    return sign_prekey_bundle(alice_crypto, unsigned_bundle)


# --------------------------------------------------------------------------- #
# Flag ON — fail-closed
# --------------------------------------------------------------------------- #


def test_flag_on_null_signature_rejected(PQ, monkeypatch, alice_keys, unsigned_bundle):
    """flag on + signature:null bundle is rejected (not stored)."""
    _, alice_pub = alice_keys
    monkeypatch.setenv("SKCHAT_REQUIRE_SIGNED_PREKEYS", "1")

    stored = PQ.store_app_prekey_bundle(
        "chef", unsigned_bundle, signer_public_armor=alice_pub
    )
    assert stored is False
    assert PQ.load_peer_bundle("chef") is None


def test_flag_on_missing_signature_key_rejected(PQ, monkeypatch, alice_keys, unsigned_bundle):
    """A bundle with no ``signature`` key at all is also rejected when flag on."""
    _, alice_pub = alice_keys
    monkeypatch.setenv("SKCHAT_REQUIRE_SIGNED_PREKEYS", "1")
    unsigned_bundle.pop("signature", None)

    stored = PQ.store_app_prekey_bundle(
        "chef", unsigned_bundle, signer_public_armor=alice_pub
    )
    assert stored is False
    assert PQ.load_peer_bundle("chef") is None


def test_flag_on_invalid_signature_rejected(PQ, monkeypatch, alice_crypto, alice_keys, unsigned_bundle):
    """flag on + a bundle failing verify_prekey_bundle is rejected (tamper)."""
    _, alice_pub = alice_keys
    monkeypatch.setenv("SKCHAT_REQUIRE_SIGNED_PREKEYS", "1")
    signed = _signed(alice_crypto, unsigned_bundle)
    # Prekey substitution: swap the hybrid public key after signing.
    signed["hybrid_public_hex"] = "cd" * 32

    stored = PQ.store_app_prekey_bundle("chef", signed, signer_public_armor=alice_pub)
    assert stored is False
    assert PQ.load_peer_bundle("chef") is None


def test_flag_on_wrong_identity_rejected(PQ, monkeypatch, alice_crypto, bob_keys, unsigned_bundle):
    """flag on + a valid signature under the WRONG identity key is rejected."""
    _, bob_pub = bob_keys
    monkeypatch.setenv("SKCHAT_REQUIRE_SIGNED_PREKEYS", "1")
    signed = _signed(alice_crypto, unsigned_bundle)  # signed by Alice

    stored = PQ.store_app_prekey_bundle("chef", signed, signer_public_armor=bob_pub)
    assert stored is False
    assert PQ.load_peer_bundle("chef") is None


def test_flag_on_no_signer_key_rejected(PQ, monkeypatch, alice_crypto, unsigned_bundle):
    """flag on + a signed bundle but no signer key to verify against → rejected."""
    monkeypatch.setenv("SKCHAT_REQUIRE_SIGNED_PREKEYS", "1")
    signed = _signed(alice_crypto, unsigned_bundle)

    stored = PQ.store_app_prekey_bundle("chef", signed, signer_public_armor=None)
    assert stored is False
    assert PQ.load_peer_bundle("chef") is None


def test_flag_on_valid_signature_accepted(PQ, monkeypatch, alice_crypto, alice_keys, unsigned_bundle):
    """flag on + a validly-signed bundle is accepted and stored."""
    _, alice_pub = alice_keys
    monkeypatch.setenv("SKCHAT_REQUIRE_SIGNED_PREKEYS", "1")
    signed = _signed(alice_crypto, unsigned_bundle)

    stored = PQ.store_app_prekey_bundle("chef", signed, signer_public_armor=alice_pub)
    assert stored is True
    got = PQ.load_peer_bundle("chef")
    assert got is not None
    assert got["hybrid_public_hex"] == unsigned_bundle["hybrid_public_hex"]
    assert got["signature"] == signed["signature"]


# --------------------------------------------------------------------------- #
# Flag OFF (default) — behaviour UNCHANGED
# --------------------------------------------------------------------------- #


def test_flag_off_default_stores_unsigned(PQ, unsigned_bundle):
    """flag off (default) leaves behaviour unchanged: unsigned bundle stored."""
    # No signer key needed and no verification performed.
    stored = PQ.store_app_prekey_bundle("chef", unsigned_bundle)
    assert stored is True
    got = PQ.load_peer_bundle("chef")
    assert got is not None
    assert got["hybrid_public_hex"] == unsigned_bundle["hybrid_public_hex"]


def test_flag_off_matches_store_peer_bundle(PQ, unsigned_bundle):
    """With the flag off the app intake normalises identically to store_peer_bundle."""
    PQ.store_app_prekey_bundle("chef", unsigned_bundle)
    via_app = PQ.load_peer_bundle("chef")

    PQ.store_peer_bundle("dave", unsigned_bundle)
    via_direct = PQ.load_peer_bundle("dave")

    assert via_app == via_direct
