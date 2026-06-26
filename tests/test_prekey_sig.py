"""RFC-0001 SOVEREIGN mode — signed hybrid prekey bundles.

Proves the *opt-in* attributable path: an agent signs its hybrid prekey bundle
with its PGP identity key so a peer can confirm the prekey belongs to the
claimed identity (closes the prekey-substitution gap). ANONYMOUS mode stays
UNSIGNED + deniable — these helpers only ADD a signed leg.

Pure helpers (``skchat.prekey_sig``); the live send path is untouched.
"""

from __future__ import annotations

import pytest

from skchat.crypto import ChatCrypto
from skchat.prekey_sig import sign_prekey_bundle, verify_prekey_bundle

PASSPHRASE = "test-passphrase-123"


@pytest.fixture()
def alice_crypto(alice_keys: tuple[str, str]) -> ChatCrypto:
    """A ChatCrypto built from the conftest alice_keys fixture."""
    private, _ = alice_keys
    return ChatCrypto(private, PASSPHRASE)


@pytest.fixture()
def sample_bundle() -> dict:
    """A hybrid prekey bundle in the pq_prekeys.agent_bundle shape (unsigned)."""
    pub_hex = "ab" * 32
    return {
        "suite": "x25519-mlkem768",
        "hybrid_public_hex": pub_hex,
        "signature": None,
        "key_id": pub_hex[:16],
        "device_id": "alice-daemon",
        "ratchet": "pqdr1",
    }


def test_sign_then_verify_true(
    alice_crypto: ChatCrypto, alice_keys: tuple[str, str], sample_bundle: dict
) -> None:
    """A bundle signed by Alice verifies True under Alice's public key."""
    _, alice_pub = alice_keys
    signed = sign_prekey_bundle(alice_crypto, sample_bundle)
    assert signed["signature"]
    assert verify_prekey_bundle(signed, alice_pub) is True


def test_verify_false_on_tamper(
    alice_crypto: ChatCrypto, alice_keys: tuple[str, str], sample_bundle: dict
) -> None:
    """Mutating hybrid_public_hex after signing breaks verification."""
    _, alice_pub = alice_keys
    signed = sign_prekey_bundle(alice_crypto, sample_bundle)
    signed["hybrid_public_hex"] = "cd" * 32
    assert verify_prekey_bundle(signed, alice_pub) is False


def test_verify_false_on_missing_signature(
    alice_keys: tuple[str, str], sample_bundle: dict
) -> None:
    """An unsigned bundle (signature None/absent) never verifies True."""
    _, alice_pub = alice_keys
    assert verify_prekey_bundle(sample_bundle, alice_pub) is False
    no_sig = dict(sample_bundle)
    no_sig.pop("signature", None)
    assert verify_prekey_bundle(no_sig, alice_pub) is False


def test_verify_false_under_different_identity(
    alice_crypto: ChatCrypto, bob_keys: tuple[str, str], sample_bundle: dict
) -> None:
    """Alice's signature does not verify under Bob's public key."""
    _, bob_pub = bob_keys
    signed = sign_prekey_bundle(alice_crypto, sample_bundle)
    assert verify_prekey_bundle(signed, bob_pub) is False
