"""Tests for DmSession — the stateful 1:1 ratchet driver (RFC-0001 P1).

DmSession wraps :class:`skchat.dm_ratchet.DmRatchet` with the epoch lifecycle:
auto-(re)key, the key-agreement message (KAM = wrapped epoch secret) piggybacked
on the first frame of each epoch (no extra round-trip), and per-frame AES-256-GCM
seal/open bound to (epoch, index). Pure state machine — no I/O.
"""

from __future__ import annotations

import pytest
from skcomms.pqkem import hybrid_keypair

from skchat.dm_session import DmSession, SealedDmFrame


def test_session_roundtrip_single_message():
    bob = hybrid_keypair()
    alice = DmSession(peer="bob")
    bob_s = DmSession(peer="alice")

    frame = alice.seal(b"hi bob", peer_hybrid_pub=bob.public_key)
    assert isinstance(frame, SealedDmFrame)
    assert frame.epoch == 0 and frame.index == 0
    assert frame.kam is not None  # first frame of the epoch carries the KAM

    assert bob_s.open(frame, my_hybrid_priv=bob.private_key) == b"hi bob"


def test_kam_only_on_first_frame_of_epoch():
    bob = hybrid_keypair()
    alice = DmSession(peer="bob")

    f0 = alice.seal(b"one", peer_hybrid_pub=bob.public_key)
    f1 = alice.seal(b"two", peer_hybrid_pub=bob.public_key)

    assert f0.kam is not None  # establishes epoch 0
    assert f1.kam is None  # same epoch — no re-key, no KAM
    assert (f0.index, f1.index) == (0, 1)


def test_multi_message_in_epoch_roundtrip():
    bob = hybrid_keypair()
    alice = DmSession(peer="bob")
    bob_s = DmSession(peer="alice")

    frames = [alice.seal(m, peer_hybrid_pub=bob.public_key) for m in (b"a", b"b", b"c")]
    out = [bob_s.open(f, my_hybrid_priv=bob.private_key) for f in frames]
    assert out == [b"a", b"b", b"c"]


def test_loss_and_reorder_tolerant_within_epoch():
    bob = hybrid_keypair()
    alice = DmSession(peer="bob")
    bob_s = DmSession(peer="alice")

    f0 = alice.seal(b"first", peer_hybrid_pub=bob.public_key)  # carries the KAM
    f1 = alice.seal(b"second", peer_hybrid_pub=bob.public_key)
    f2 = alice.seal(b"third", peer_hybrid_pub=bob.public_key)

    # Once the KAM-bearing first frame is in (reliable transport delivers it), the
    # rest open in any order and a loss (f1 never arrives) is fine — index-addressed.
    assert bob_s.open(f0, my_hybrid_priv=bob.private_key) == b"first"
    assert bob_s.open(f2, my_hybrid_priv=bob.private_key) == b"third"  # reorder, f1 lost
    _ = f1  # deliberately dropped


def test_auto_rekey_starts_new_epoch_with_pcs():
    bob = hybrid_keypair()
    alice = DmSession(peer="bob", rekey_msg_bound=2)
    bob_s = DmSession(peer="alice")

    f0 = alice.seal(b"e0m0", peer_hybrid_pub=bob.public_key)  # epoch 0
    f1 = alice.seal(b"e0m1", peer_hybrid_pub=bob.public_key)  # epoch 0 (now at bound)
    f2 = alice.seal(b"e1m0", peer_hybrid_pub=bob.public_key)  # triggers rekey -> epoch 1

    assert f0.epoch == 0 and f1.epoch == 0
    assert f2.epoch == 1 and f2.kam is not None  # rekey -> fresh KAM (PQ heal)

    assert bob_s.open(f0, my_hybrid_priv=bob.private_key) == b"e0m0"
    assert bob_s.open(f2, my_hybrid_priv=bob.private_key) == b"e1m0"

    # The two epochs use independent secrets (post-compromise security).
    assert alice._epoch_secret_for_test(0) != alice._epoch_secret_for_test(1)


def test_open_rejects_tampered_body():
    bob = hybrid_keypair()
    alice = DmSession(peer="bob")
    bob_s = DmSession(peer="alice")

    frame = alice.seal(b"authentic", peer_hybrid_pub=bob.public_key)
    tampered = SealedDmFrame(
        epoch=frame.epoch,
        index=frame.index,
        nonce=frame.nonce,
        body=frame.body[:-1] + bytes([frame.body[-1] ^ 0x01]),
        kam=frame.kam,
    )
    with pytest.raises(Exception):
        bob_s.open(tampered, my_hybrid_priv=bob.private_key)
