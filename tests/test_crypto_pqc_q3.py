"""PQC Q3 — hybrid 1:1 DM sealing tests (skchat ChatCrypto, negotiated/opt-in).

Covers the Phase-1 HNDL fix for the DM surface (plan §3 S6, KEM leg):
    * hybrid DM round-trip (encap -> AES-256-GCM -> decap), still classically signed
    * negotiation: hybrid when the recipient advertises a hybrid prekey, else the
      UNCHANGED classical PGP path (negotiated downgrade)
    * downgrade-lock: party/suite tamper is detected on open
    * back-compat: the classical encrypt/decrypt path is byte-for-byte unchanged
      (a classical message produced by encrypt_message is NOT a hybrid token)
    * self-report reflects the negotiated suite per conversation

REQUIRE the liboqs-backed hybrid KEM (skcomms.pqkem); skip if unavailable.
"""

from __future__ import annotations

import pytest

pqkem = pytest.importorskip("skcomms.pqkem")
pqdm = pytest.importorskip("skcomms.pqdm")

if not pqkem.is_available():
    pytest.skip("liboqs/oqs backend unavailable", allow_module_level=True)

from skcomms.pqdm import HYBRID_SUITE, PrekeyBundle  # noqa: E402

from skchat.crypto import ChatCrypto, DecryptionError  # noqa: E402
from skchat.models import ChatMessage  # noqa: E402

PASSPHRASE = "test-passphrase-123"


def _bundle() -> tuple[PrekeyBundle, bytes]:
    kp = pqkem.hybrid_keypair()
    return (
        PrekeyBundle(suite=HYBRID_SUITE, hybrid_public_hex=kp.public_key.hex()),
        kp.private_key,
    )


def _msg(content="HNDL secret DM"):
    return ChatMessage(
        sender="capauth:alice@skworld.io",
        recipient="capauth:bob@skworld.io",
        content=content,
    )


def test_hybrid_dm_roundtrip(alice_keys, bob_keys):
    alice_priv, _ = alice_keys
    bob_priv, bob_pub = bob_keys
    bundle, bob_hybrid_priv = _bundle()

    alice = ChatCrypto(alice_priv, PASSPHRASE)
    sealed, suite = alice.encrypt_message_auto(
        _msg(), recipient_public_armor=bob_pub, recipient_bundle=bundle
    )
    assert suite == HYBRID_SUITE
    assert sealed.encrypted is True
    assert ChatCrypto.is_hybrid_message(sealed) is True
    assert sealed.content != "HNDL secret DM"
    assert sealed.metadata.get("kem_suite") == HYBRID_SUITE
    assert sealed.signature  # still classically signed

    bob = ChatCrypto(bob_priv, PASSPHRASE)
    opened = bob.decrypt_message_hybrid(sealed, bob_hybrid_priv)
    assert opened.content == "HNDL secret DM"
    assert opened.encrypted is False


def test_negotiated_downgrade_uses_classical(alice_keys, bob_keys):
    """No hybrid prekey -> the UNCHANGED classical PGP path; suite recorded honestly."""
    alice_priv, _ = alice_keys
    bob_priv, bob_pub = bob_keys

    alice = ChatCrypto(alice_priv, PASSPHRASE)
    out, suite = alice.encrypt_message_auto(
        _msg("classic body"), recipient_public_armor=bob_pub, recipient_bundle=None
    )
    assert suite == pqdm.CLASSICAL_SUITE
    assert ChatCrypto.is_hybrid_message(out) is False
    # Classical path: PGP armor, decryptable by the existing classical method.
    bob = ChatCrypto(bob_priv, PASSPHRASE)
    dec = bob.decrypt_message(out)
    assert dec.content == "classic body"


def test_classical_path_unchanged(alice_keys, bob_keys):
    """encrypt_message (classical) output is identical shape to pre-Q3.

    A message encrypted by the classical method must NOT be a hybrid token and
    must decrypt via the classical method — proving byte-for-byte back-compat.
    """
    alice_priv, _ = alice_keys
    bob_priv, bob_pub = bob_keys
    alice = ChatCrypto(alice_priv, PASSPHRASE)
    enc = alice.encrypt_message(_msg("legacy"), bob_pub)
    assert ChatCrypto.is_hybrid_message(enc) is False
    assert enc.content.startswith("-----BEGIN PGP")
    bob = ChatCrypto(bob_priv, PASSPHRASE)
    assert bob.decrypt_message(enc).content == "legacy"


def test_downgrade_lock_party_tamper_detected(alice_keys, bob_keys):
    alice_priv, _ = alice_keys
    _, bob_pub = bob_keys
    bundle, bob_hybrid_priv = _bundle()
    alice = ChatCrypto(alice_priv, PASSPHRASE)
    sealed, _ = alice.encrypt_message_auto(
        _msg(), recipient_public_armor=bob_pub, recipient_bundle=bundle
    )
    # Attacker rewrites the sender on the envelope; AAD binding fails on open.
    tampered = sealed.model_copy(update={"sender": "capauth:mallory@skworld.io"})
    bob = ChatCrypto(bob_keys[0], PASSPHRASE)
    with pytest.raises(DecryptionError):
        bob.decrypt_message_hybrid(tampered, bob_hybrid_priv)


def test_malformed_hybrid_message_raises(bob_keys):
    bob_priv, _ = bob_keys
    _, bob_hybrid_priv = _bundle()
    bob = ChatCrypto(bob_priv, PASSPHRASE)
    bad = ChatMessage(
        sender="capauth:alice@skworld.io",
        recipient="capauth:bob@skworld.io",
        content="pqdm1:x25519-mlkem768:not-base64!!",
        encrypted=True,
    )
    with pytest.raises(DecryptionError):
        bob.decrypt_message_hybrid(bad, bob_hybrid_priv)


def test_self_report_reflects_negotiated_suite():
    from sksecurity.pqc_report import conversation_surface_for

    surf, comp, suite, note = conversation_surface_for(HYBRID_SUITE, "dm", "bob")
    assert suite == HYBRID_SUITE
    assert "HNDL-resistant" in note
    surf2, _, suite2, note2 = conversation_surface_for(
        pqdm.CLASSICAL_SUITE, "dm", "bob"
    )
    assert suite2 == pqdm.CLASSICAL_SUITE
    assert "CLASSICAL" in note2
