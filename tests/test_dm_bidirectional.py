"""TRUE interleaved bidirectional DM ratchet on ONE shared per-peer session.

RFC-0001 P1 limitation fix: a :class:`skchat.dm_session.DmSession` used to key all
epoch secrets by epoch *number* in a single dict shared by BOTH the send and the
receive chains. On a single shared per-peer session that already sealed at epoch 0,
the peer's own epoch-0 KAM was therefore ignored (its epoch number already had a
[send] secret), so true interleaved bidirectional traffic on ONE session per side
was impossible — the e2e integration test had to work around it with separate
per-direction session stores.

The fix gives the session **separate send/recv epoch namespaces** so each direction
re-keys independently. These tests pin the real-world shape the live skchat/skcomms
path uses: ONE :class:`DmSession` per peer on each side, both sealing AND opening,
across multiple epochs. The wire format (``pqdr1:`` token) is unchanged.
"""

from __future__ import annotations

import pytest
from skcomms.pqkem import hybrid_keypair

from skchat.dm_session import DmSession, SealedDmFrame


def test_true_interleaved_bidirectional_single_session():
    """A & B each hold ONE shared session; both seal and open, interleaved."""
    a_kp = hybrid_keypair()
    b_kp = hybrid_keypair()

    # ONE session per side — the realistic live shape (manager keeps one per peer).
    alice = DmSession(peer="bob")
    bob = DmSession(peer="alice")

    # Interleave: each side seals BEFORE opening the other's first frame.
    fa = alice.seal(b"a->b 1", peer_hybrid_pub=b_kp.public_key)  # alice send epoch 0
    fb = bob.seal(b"b->a 1", peer_hybrid_pub=a_kp.public_key)    # bob send epoch 0

    # The crux: alice already sealed at epoch 0, yet must still re-key from bob's
    # epoch-0 KAM (separate recv namespace) — and symmetrically for bob.
    assert alice.open(fb, my_hybrid_priv=a_kp.private_key) == b"b->a 1"
    assert bob.open(fa, my_hybrid_priv=b_kp.private_key) == b"a->b 1"

    # Keep going both ways on the SAME two sessions (still epoch 0 each direction).
    fa2 = alice.seal(b"a->b 2", peer_hybrid_pub=b_kp.public_key)
    fb2 = bob.seal(b"b->a 2", peer_hybrid_pub=a_kp.public_key)
    assert bob.open(fa2, my_hybrid_priv=b_kp.private_key) == b"a->b 2"
    assert alice.open(fb2, my_hybrid_priv=a_kp.private_key) == b"b->a 2"


def test_interleaved_bidirectional_multiple_epochs():
    """Both directions cross epoch boundaries independently on one session each."""
    a_kp = hybrid_keypair()
    b_kp = hybrid_keypair()

    # rekey after every 2 messages so both send chains roll epochs repeatedly.
    alice = DmSession(peer="bob", rekey_msg_bound=2)
    bob = DmSession(peer="alice", rekey_msg_bound=2)

    a_seen: list[bytes] = []
    b_seen: list[bytes] = []
    for i in range(6):
        fa = alice.seal(f"a{i}".encode(), peer_hybrid_pub=b_kp.public_key)
        fb = bob.seal(f"b{i}".encode(), peer_hybrid_pub=a_kp.public_key)
        # open the OTHER side's frame on the same shared session (interleaved)
        b_seen.append(bob.open(fa, my_hybrid_priv=b_kp.private_key))
        a_seen.append(alice.open(fb, my_hybrid_priv=a_kp.private_key))

    assert b_seen == [f"a{i}".encode() for i in range(6)]
    assert a_seen == [f"b{i}".encode() for i in range(6)]

    # Each side's SEND chain advanced through several independent epochs (rekey),
    # while its RECV chain tracked the peer's independent epochs in parallel.
    # 6 msgs @ bound 2 → send epochs 0,1,2 used on each side.
    last_a = SealedDmFrame.from_token(
        alice.seal(b"a-final", peer_hybrid_pub=b_kp.public_key).to_token()
    )
    assert last_a.epoch >= 2  # send chain genuinely re-keyed multiple times


def test_send_recv_epoch_secrets_are_independent_namespaces():
    """A peer's recv epoch-0 secret must NOT be the session's own send epoch-0
    secret — distinct keyspaces, so neither direction clobbers the other."""
    a_kp = hybrid_keypair()
    b_kp = hybrid_keypair()
    alice = DmSession(peer="bob")
    bob = DmSession(peer="alice")

    alice.seal(b"x", peer_hybrid_pub=b_kp.public_key)            # alice send epoch 0
    fb = bob.seal(b"y", peer_hybrid_pub=a_kp.public_key)         # bob send epoch 0
    alice.open(fb, my_hybrid_priv=a_kp.private_key)              # alice recv epoch 0

    send0 = alice._send_epoch_secret_for_test(0)
    recv0 = alice._recv_epoch_secret_for_test(0)
    assert send0 is not None and recv0 is not None
    assert send0 != recv0  # independent secrets for the two directions


def test_interleaved_bidirectional_survives_snapshot_restore():
    """The split send/recv namespaces persist + restore, continuing both chains."""
    a_kp = hybrid_keypair()
    b_kp = hybrid_keypair()
    alice = DmSession(peer="bob")
    bob = DmSession(peer="alice")

    fa = alice.seal(b"a1", peer_hybrid_pub=b_kp.public_key)
    fb = bob.seal(b"b1", peer_hybrid_pub=a_kp.public_key)
    assert alice.open(fb, my_hybrid_priv=a_kp.private_key) == b"b1"
    assert bob.open(fa, my_hybrid_priv=b_kp.private_key) == b"a1"

    # Round-trip alice through snapshot/restore (daemon restart) — both her send
    # ratchet (epoch/index) AND her learned recv epoch-0 secret must survive.
    alice2 = DmSession.restore(alice.snapshot())

    # Same-epoch follow-ups open with no fresh KAM (recv secret restored), and the
    # send chain continues without index reuse.
    fb2 = bob.seal(b"b2", peer_hybrid_pub=a_kp.public_key)  # bob still epoch 0, no KAM
    assert alice2.open(fb2, my_hybrid_priv=a_kp.private_key) == b"b2"
    fa2 = alice2.seal(b"a2", peer_hybrid_pub=b_kp.public_key)
    assert SealedDmFrame.from_token(fa2.to_token()).index == 1  # continued send chain
    assert bob.open(fa2, my_hybrid_priv=b_kp.private_key) == b"a2"


def test_legacy_v1_snapshot_still_restores():
    """A pre-fix (v1) snapshot — single ``epoch_secrets`` dict — still restores and
    its session keeps working (backward compatible: old sealed stores must load)."""
    a_kp = hybrid_keypair()
    b_kp = hybrid_keypair()

    # Build a real session, then hand-craft its OLD v1 snapshot shape.
    s = DmSession(peer="bob")
    s.seal(b"m0", peer_hybrid_pub=b_kp.public_key)
    s.seal(b"m1", peer_hybrid_pub=b_kp.public_key)
    new_snap = s.snapshot()
    legacy_snap = {
        "v": 1,
        "peer": new_snap["peer"],
        "rekey_msg_bound": new_snap["rekey_msg_bound"],
        "rekey_age_seconds": new_snap["rekey_age_seconds"],
        # the old single namespace == this session's send secrets
        "epoch_secrets": {
            "0": s._send_epoch_secret_for_test(0).hex(),
        },
        "current_kam": new_snap["current_kam"],
        "ratchet": new_snap["ratchet"],
    }

    restored = DmSession.restore(legacy_snap)
    # Continues the send chain (index 2, no reuse) — proves the legacy secret was
    # mapped into the send namespace and the ratchet rebuilt.
    f2 = restored.seal(b"m2", peer_hybrid_pub=b_kp.public_key)
    assert SealedDmFrame.from_token(f2.to_token()).index == 2

    bob = DmSession(peer="alice")
    assert bob.open(f2, my_hybrid_priv=b_kp.private_key) == b"m2"


@pytest.mark.e2e_live
def test_manager_single_shared_session_bidirectional(tmp_path, monkeypatch):
    """Live path: DmRatchetManager keeps ONE session per peer; true interleaved
    bidirectional now works through it WITHOUT separate per-direction stores."""
    pqkem = pytest.importorskip("skcomms.pqkem")
    if not pqkem.is_available():
        pytest.skip("liboqs/oqs backend unavailable")

    from skchat.crypto import ChatCrypto
    from skchat.dm_manager import DmRatchetManager
    from skchat.models import ChatMessage
    from tests.conftest import PASSPHRASE, _generate_test_keypair

    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path / "home"))
    from skchat import pq_prekeys as pk

    # Any valid ChatCrypto serves both sides (ratchet methods don't touch PGP).
    priv, _ = _generate_test_keypair("Lumina", "lumina@skworld.io")
    crypto = ChatCrypto(priv, PASSPHRASE)

    pk.ensure_agent_keypair("lumina")
    pk.ensure_agent_keypair("jarvis")
    pk.store_peer_bundle("jarvis", pk.agent_bundle("jarvis"))
    pk.store_peer_bundle("lumina", pk.agent_bundle("lumina"))

    # ONE manager (hence ONE session store) per agent — the live shape.
    lumina = DmRatchetManager.for_agent(crypto, "lumina", tmp_path / "lumina")
    jarvis = DmRatchetManager.for_agent(crypto, "jarvis", tmp_path / "jarvis")
    assert lumina is not None and jarvis is not None

    # Interleave: both seal before opening the other's frame, on the SAME stores.
    l_out = lumina.seal(ChatMessage(sender="lumina", recipient="jarvis", content="L1"))
    j_out = jarvis.seal(ChatMessage(sender="jarvis", recipient="lumina", content="J1"))
    assert ChatCrypto.is_ratchet_message(l_out) and ChatCrypto.is_ratchet_message(j_out)

    # lumina's single jarvis-session both sent (L1) AND now opens jarvis's J1.
    assert lumina.open(j_out).content == "J1"
    assert jarvis.open(l_out).content == "L1"

    # Keep going both ways on the same persisted sessions.
    l2 = lumina.seal(ChatMessage(sender="lumina", recipient="jarvis", content="L2"))
    j2 = jarvis.seal(ChatMessage(sender="jarvis", recipient="lumina", content="J2"))
    assert jarvis.open(l2).content == "L2"
    assert lumina.open(j2).content == "J2"
