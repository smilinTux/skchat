"""P0.2 — decouple classical PGP signing from ratchet confidentiality.

``ChatCrypto`` gains a ``can_sign`` flag and a ``without_signing_key`` factory:
a ratchet-only engine whose AEAD-based (unsigned, deniable) DM ratchet works
with a hybrid key, while classical PGP signing/encryption is unavailable.
``load_agent_crypto`` returns such a ratchet-only engine — instead of ``None`` —
when the agent's PGP key is missing, so confidentiality is never disabled just
because signing degraded.
"""

from __future__ import annotations

import pytest
from skcomms.pqkem import hybrid_keypair

from skchat.crypto import ChatCrypto, SigningError, load_agent_crypto
from skchat.dm_manager import DmRatchetManager
from skchat.dm_store import DmSessionStore
from skchat.models import ChatMessage


def test_normal_crypto_can_sign(alice_keys):
    crypto = ChatCrypto(alice_keys[0], "")
    assert crypto.can_sign is True


def test_ratchet_only_crypto_cannot_sign():
    crypto = ChatCrypto.without_signing_key()
    assert crypto.can_sign is False


def test_ratchet_only_sign_message_raises():
    crypto = ChatCrypto.without_signing_key()
    msg = ChatMessage(sender="a", recipient="b", content="x")
    with pytest.raises(SigningError):
        crypto.sign_message(msg)


def _manager(crypto, tmp_path, *, me, my_kp, peer_pubs):
    store = DmSessionStore(tmp_path / f"{me}.db")
    return DmRatchetManager(
        crypto,
        agent_public=my_kp.public_key,
        agent_private=my_kp.private_key,
        peer_pub_resolver=lambda p: peer_pubs.get(p),
        store=store,
        store_key=b"\x11" * 32,
    )


def test_ratchet_roundtrip_with_signing_degraded_crypto(tmp_path):
    """A ratchet-only ChatCrypto (no PGP key) still seals/opens with a hybrid key."""
    crypto = ChatCrypto.without_signing_key()
    a_kp, b_kp = hybrid_keypair(), hybrid_keypair()
    alice = _manager(crypto, tmp_path, me="alice", my_kp=a_kp, peer_pubs={"bob": b_kp.public_key})
    bob = _manager(crypto, tmp_path, me="bob", my_kp=b_kp, peer_pubs={"alice": a_kp.public_key})

    sealed = alice.seal(ChatMessage(sender="alice", recipient="bob", content="hi over ratchet"))
    assert ChatCrypto.is_ratchet_message(sealed)
    assert bob.open(sealed).content == "hi over ratchet"


def test_load_agent_crypto_returns_ratchet_only_when_key_missing(monkeypatch, tmp_path):
    """Missing PGP key → a ratchet-only ChatCrypto (not None) so confidentiality survives."""
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.skcapstone/agents/<agent>/... key
    crypto = load_agent_crypto("capauth:nobody-xyz-9931@skworld.io")
    assert crypto is not None
    assert crypto.can_sign is False
