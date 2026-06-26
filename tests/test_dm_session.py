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


def test_kam_repeated_on_first_frames_then_stops():
    """The KAM rides the first few frames of an epoch (robust to a lost/reordered
    first frame over a reliable transport), then stops (per-epoch amortisation)."""
    bob = hybrid_keypair()
    alice = DmSession(peer="bob")

    frames = [alice.seal(f"m{i}".encode(), peer_hybrid_pub=bob.public_key) for i in range(5)]

    from skchat.dm_session import _KAM_REPEAT

    for i in range(_KAM_REPEAT):
        assert frames[i].kam is not None, f"frame {i} should carry the KAM"
    assert frames[_KAM_REPEAT].kam is None  # KAM stops after the repeat window
    assert [f.index for f in frames] == [0, 1, 2, 3, 4]


def test_reordered_kam_frame_establishes_epoch():
    """A later KAM-bearing frame can establish the epoch if the very first is lost."""
    bob = hybrid_keypair()
    alice = DmSession(peer="bob")
    bob_s = DmSession(peer="alice")

    f0 = alice.seal(b"first", peer_hybrid_pub=bob.public_key)
    f1 = alice.seal(b"second", peer_hybrid_pub=bob.public_key)  # also carries the KAM

    # f0 is lost; f1 (carrying a repeated KAM) arrives first and still establishes.
    assert f1.kam is not None
    assert bob_s.open(f1, my_hybrid_priv=bob.private_key) == b"second"
    _ = f0


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
