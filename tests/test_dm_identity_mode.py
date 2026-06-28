"""RFC-0001 §2.1 — DM identity mode switch (ANONYMOUS ↔ SOVEREIGN).

Adds a per-manager auth ``mode`` to :class:`skchat.dm_manager.DmRatchetManager`:

* **ANONYMOUS** (default) — current behaviour: deniable, unsigned, no DID binding.
  The peer's hybrid prekey is ratcheted as-resolved; nothing is verified.
* **SOVEREIGN** (opt-in) — attributable: the peer's prekey bundle MUST carry a
  valid identity signature (verified via :mod:`skchat.prekey_sig`) before the
  manager will ratchet to it. An unsigned / tampered / wrong-identity bundle is
  refused → no ratchet (honest classical fallback, never a silent downgrade).

Invariant under test: the **ratchet steps stay signature-free in BOTH modes**, so
content deniability survives even in sovereign mode — a sovereign-sealed frame
opens in an anonymous manager (identity is asserted only at establishment, never
per-message).
"""

from __future__ import annotations

import pytest
from skcomms.pqkem import hybrid_keypair

from skchat.crypto import ChatCrypto
from skchat.dm_manager import AuthMode, DmRatchetManager
from skchat.dm_store import DmSessionStore
from skchat.models import ChatMessage
from skchat.prekey_sig import sign_prekey_bundle

PASSPHRASE = "test-passphrase-123"


@pytest.fixture(scope="module")
def crypto(alice_keys):
    # Ratchet methods don't touch the PGP key — any ChatCrypto is a method-holder.
    return ChatCrypto(alice_keys[0], PASSPHRASE)


def _bundle(pub_hex: str, *, device: str = "bob-daemon") -> dict:
    return {
        "suite": "x25519-mlkem768",
        "hybrid_public_hex": pub_hex,
        "signature": None,
        "key_id": pub_hex[:16],
        "device_id": device,
        "ratchet": "pqdr1",
    }


def _manager(
    crypto,
    tmp_path,
    *,
    me,
    my_kp,
    mode=AuthMode.ANONYMOUS,
    peer_pubs=None,
    peer_bundles=None,
    peer_idents=None,
):
    store = DmSessionStore(tmp_path / f"{me}.db")
    peer_pubs = peer_pubs or {}
    peer_bundles = peer_bundles or {}
    peer_idents = peer_idents or {}
    return DmRatchetManager(
        crypto,
        agent_public=my_kp.public_key,
        agent_private=my_kp.private_key,
        peer_pub_resolver=lambda p: peer_pubs.get(p),
        store=store,
        store_key=b"\x11" * 32,
        mode=mode,
        peer_bundle_resolver=lambda p: peer_bundles.get(p),
        peer_identity_resolver=lambda p: peer_idents.get(p),
    )


# --- mode is selectable + anonymous default -------------------------------- #


def test_default_mode_is_anonymous(crypto, tmp_path):
    """No mode arg → ANONYMOUS, and the legacy constructor signature still works."""
    a_kp = hybrid_keypair()
    store = DmSessionStore(tmp_path / "a.db")
    mgr = DmRatchetManager(
        crypto,
        agent_public=a_kp.public_key,
        agent_private=a_kp.private_key,
        peer_pub_resolver=lambda p: None,
        store=store,
        store_key=b"\x11" * 32,
    )
    assert mgr.mode is AuthMode.ANONYMOUS


def test_mode_is_selectable(crypto, tmp_path):
    a_kp = hybrid_keypair()
    mgr = _manager(crypto, tmp_path, me="alice", my_kp=a_kp, mode=AuthMode.SOVEREIGN)
    assert mgr.mode is AuthMode.SOVEREIGN


# --- anonymous: unchanged behaviour ---------------------------------------- #


def test_anonymous_roundtrip_unchanged(crypto, tmp_path):
    """ANONYMOUS mode ratchets on the bare resolved pub — no signature needed."""
    a_kp, b_kp = hybrid_keypair(), hybrid_keypair()
    alice = _manager(
        crypto, tmp_path, me="alice", my_kp=a_kp, peer_pubs={"bob": b_kp.public_key}
    )
    bob = _manager(
        crypto, tmp_path, me="bob", my_kp=b_kp, peer_pubs={"alice": a_kp.public_key}
    )
    assert alice.can_ratchet("bob") is True
    sealed = alice.seal(ChatMessage(sender="alice", recipient="bob", content="hi anon"))
    assert sealed.encrypted
    assert bob.open(sealed).content == "hi anon"


# --- sovereign: requires + verifies a valid signature ---------------------- #


def test_sovereign_ratchets_with_valid_signature(crypto, tmp_path, bob_keys):
    bob_priv, bob_pub = bob_keys
    bob_crypto = ChatCrypto(bob_priv, PASSPHRASE)
    a_kp, b_kp = hybrid_keypair(), hybrid_keypair()
    signed = sign_prekey_bundle(bob_crypto, _bundle(b_kp.public_key.hex()))

    alice = _manager(
        crypto,
        tmp_path,
        me="alice",
        my_kp=a_kp,
        mode=AuthMode.SOVEREIGN,
        peer_bundles={"bob": signed},
        peer_idents={"bob": bob_pub},
    )
    bob = _manager(  # bob opens in anonymous mode — ratchet is identity-free
        crypto, tmp_path, me="bob", my_kp=b_kp, peer_pubs={"alice": a_kp.public_key}
    )

    assert alice.can_ratchet("bob") is True
    sealed = alice.seal(ChatMessage(sender="alice", recipient="bob", content="sov hello"))
    assert sealed.encrypted
    assert bob.open(sealed).content == "sov hello"  # signature-free ratchet → opens anon


def test_sovereign_rejects_unsigned_bundle(crypto, tmp_path, bob_keys):
    _, bob_pub = bob_keys
    a_kp, b_kp = hybrid_keypair(), hybrid_keypair()
    unsigned = _bundle(b_kp.public_key.hex())  # signature stays None

    alice = _manager(
        crypto,
        tmp_path,
        me="alice",
        my_kp=a_kp,
        mode=AuthMode.SOVEREIGN,
        peer_bundles={"bob": unsigned},
        peer_idents={"bob": bob_pub},
    )
    assert alice.can_ratchet("bob") is False
    msg = ChatMessage(sender="alice", recipient="bob", content="should not ratchet")
    out = alice.seal(msg)
    assert out is msg  # untouched → caller takes classical path (no silent downgrade)


def test_sovereign_rejects_tampered_signature(crypto, tmp_path, bob_keys):
    bob_priv, bob_pub = bob_keys
    bob_crypto = ChatCrypto(bob_priv, PASSPHRASE)
    a_kp, b_kp = hybrid_keypair(), hybrid_keypair()
    signed = sign_prekey_bundle(bob_crypto, _bundle(b_kp.public_key.hex()))
    signed = dict(signed)
    signed["hybrid_public_hex"] = "cd" * 1216  # swap the prekey after signing

    alice = _manager(
        crypto,
        tmp_path,
        me="alice",
        my_kp=a_kp,
        mode=AuthMode.SOVEREIGN,
        peer_bundles={"bob": signed},
        peer_idents={"bob": bob_pub},
    )
    assert alice.can_ratchet("bob") is False


def test_sovereign_rejects_wrong_identity(crypto, tmp_path, alice_keys, bob_keys):
    """A bundle signed by Bob but presented under Alice's identity is refused."""
    bob_priv, _ = bob_keys
    _, alice_pub = alice_keys
    bob_crypto = ChatCrypto(bob_priv, PASSPHRASE)
    a_kp, b_kp = hybrid_keypair(), hybrid_keypair()
    signed = sign_prekey_bundle(bob_crypto, _bundle(b_kp.public_key.hex()))

    alice = _manager(
        crypto,
        tmp_path,
        me="alice",
        my_kp=a_kp,
        mode=AuthMode.SOVEREIGN,
        peer_bundles={"bob": signed},
        peer_idents={"bob": alice_pub},  # wrong identity for this signature
    )
    assert alice.can_ratchet("bob") is False


def test_sovereign_fails_closed_without_resolvers(crypto, tmp_path):
    """SOVEREIGN with no bundle/identity resolver → refuse (fail closed), no crash."""
    a_kp = hybrid_keypair()
    mgr = _manager(crypto, tmp_path, me="alice", my_kp=a_kp, mode=AuthMode.SOVEREIGN)
    assert mgr.can_ratchet("bob") is False
