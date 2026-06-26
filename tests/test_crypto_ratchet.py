"""ChatCrypto ratchet methods — wiring DmSession onto the message path.

Mirrors the hybrid one-shot pair (``encrypt_message_hybrid`` /
``decrypt_message_hybrid``) but drives the stateful per-epoch
:class:`skchat.dm_session.DmSession` ratchet (forward secrecy + PQ rekey heal),
storing the sealed frame as a ``pqdr1:`` token in ``ChatMessage.content``.

REQUIRE the liboqs-backed hybrid KEM (skcomms.pqkem); skip if unavailable.
"""

from __future__ import annotations

import pytest

pqkem = pytest.importorskip("skcomms.pqkem")

if not pqkem.is_available():
    pytest.skip("liboqs/oqs backend unavailable", allow_module_level=True)

from skchat.crypto import ChatCrypto  # noqa: E402
from skchat.dm_session import PQDR_SCHEME, DmSession  # noqa: E402
from skchat.models import ChatMessage  # noqa: E402

PASSPHRASE = "test-passphrase-123"


def _msg(content="ratchet secret DM"):
    return ChatMessage(
        sender="capauth:alice@skworld.io",
        recipient="capauth:bob@skworld.io",
        content=content,
    )


def test_ratchet_roundtrip_alice_to_bob(alice_keys, bob_keys):
    alice_priv, _ = alice_keys
    bob_priv, _ = bob_keys
    bob_kp = pqkem.hybrid_keypair()

    alice = ChatCrypto(alice_priv, PASSPHRASE)
    bob = ChatCrypto(bob_priv, PASSPHRASE)
    alice_sess = DmSession(peer="bob")
    bob_sess = DmSession(peer="alice")

    sealed = alice.encrypt_message_ratchet(_msg(), alice_sess, bob_kp.public_key)
    assert sealed.encrypted is True
    assert sealed.content.startswith(PQDR_SCHEME)
    assert sealed.content != "ratchet secret DM"
    assert ChatCrypto.is_ratchet_message(sealed) is True
    assert sealed.metadata.get("kem_suite") == "x25519-mlkem768"
    assert sealed.metadata.get("ratchet") == "dm-epoch"

    opened = bob.decrypt_message_ratchet(sealed, bob_sess, bob_kp.private_key)
    assert opened.content == "ratchet secret DM"
    assert opened.encrypted is False


def test_ratchet_multi_message_same_epoch(alice_keys, bob_keys):
    alice = ChatCrypto(alice_keys[0], PASSPHRASE)
    bob = ChatCrypto(bob_keys[0], PASSPHRASE)
    bob_kp = pqkem.hybrid_keypair()
    a_sess = DmSession(peer="bob")
    b_sess = DmSession(peer="alice")

    out = []
    for text in ("one", "two", "three"):
        s = alice.encrypt_message_ratchet(_msg(text), a_sess, bob_kp.public_key)
        out.append(bob.decrypt_message_ratchet(s, b_sess, bob_kp.private_key).content)
    assert out == ["one", "two", "three"]


def test_is_ratchet_message_false_for_plain(alice_keys):
    assert ChatCrypto.is_ratchet_message(_msg("plain")) is False


def test_decrypt_ratchet_rejects_non_ratchet(alice_keys, bob_keys):
    from skchat.crypto import DecryptionError

    bob = ChatCrypto(bob_keys[0], PASSPHRASE)
    bob_kp = pqkem.hybrid_keypair()
    bad = _msg("not a token").model_copy(update={"encrypted": True})
    with pytest.raises(DecryptionError):
        bob.decrypt_message_ratchet(bad, DmSession(peer="alice"), bob_kp.private_key)
