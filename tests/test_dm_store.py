"""Tests for DmSession persistence (RFC-0001 P1 — survive daemon restarts).

A conversation's ratchet state (epoch secrets, current epoch/index) must persist so
a restart doesn't lose the ratchet — but the epoch secrets are key material, so they
MUST be sealed at rest (AES-256-GCM under a caller-supplied key), never plaintext.
"""

from __future__ import annotations

import pytest
from skcomms.pqkem import hybrid_keypair

from skchat.dm_session import DmSession
from skchat.dm_store import DmSessionStore


def test_snapshot_restore_continues_index_without_reuse():
    bob = hybrid_keypair()
    s = DmSession(peer="bob")
    s.seal(b"m0", peer_hybrid_pub=bob.public_key)  # epoch 0, index 0
    s.seal(b"m1", peer_hybrid_pub=bob.public_key)  # epoch 0, index 1

    restored = DmSession.restore(s.snapshot())
    frame = restored.seal(b"m2", peer_hybrid_pub=bob.public_key)

    assert frame.epoch == 0 and frame.index == 2  # no index reuse across restart


def test_restored_receiver_still_opens_same_epoch_frames():
    bob = hybrid_keypair()
    alice = DmSession(peer="bob")
    bob_s = DmSession(peer="alice")

    f0 = alice.seal(b"hi", peer_hybrid_pub=bob.public_key)
    assert bob_s.open(f0, my_hybrid_priv=bob.private_key) == b"hi"  # learns epoch-0 secret

    bob_restored = DmSession.restore(bob_s.snapshot())
    f1 = alice.seal(b"again", peer_hybrid_pub=bob.public_key)  # same epoch, no KAM
    assert bob_restored.open(f1, my_hybrid_priv=bob.private_key) == b"again"


def test_store_seals_epoch_secret_at_rest(tmp_path):
    key = b"\x07" * 32
    bob = hybrid_keypair()
    s = DmSession(peer="bob")
    s.seal(b"secret message", peer_hybrid_pub=bob.public_key)
    epoch_secret = s._epoch_secret_for_test(0)

    store = DmSessionStore(tmp_path / "dm.db")
    store.save(s, key)

    raw = (tmp_path / "dm.db").read_bytes()
    assert epoch_secret not in raw  # epoch secret is NEVER plaintext on disk

    loaded = store.load("bob", key)
    assert loaded is not None
    assert loaded._epoch_secret_for_test(0) == epoch_secret  # recovered under the key


def test_store_wrong_key_rejected(tmp_path):
    bob = hybrid_keypair()
    s = DmSession(peer="bob")
    s.seal(b"x", peer_hybrid_pub=bob.public_key)

    store = DmSessionStore(tmp_path / "dm.db")
    store.save(s, b"\x07" * 32)

    with pytest.raises(Exception):
        store.load("bob", b"\x08" * 32)  # wrong key -> AEAD auth failure


def test_store_load_missing_peer_returns_none(tmp_path):
    store = DmSessionStore(tmp_path / "dm.db")
    assert store.load("nobody", b"\x07" * 32) is None
