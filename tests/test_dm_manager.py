"""Tests for DmRatchetManager — the live-path orchestration (RFC-0001 P1 cutover).

Ties together: the agent's hybrid keypair, peer prekey resolution, the sealed
DmSessionStore, and ChatCrypto's ratchet methods — exposing the two calls the
transport needs (seal an outbound DM, open an inbound one) with honest fallback
(classical/hybrid-one-shot messages pass straight through; no peer prekey → no
ratchet). Injectable so it's tested without touching the filesystem/env.
"""

from __future__ import annotations

import pytest
from skcomms.pqkem import hybrid_keypair

from skchat.crypto import ChatCrypto
from skchat.dm_manager import DmRatchetManager
from skchat.dm_store import DmSessionStore
from skchat.models import ChatMessage


@pytest.fixture(scope="module")
def crypto(alice_keys):
    # The ratchet methods don't use the PGP key — any valid ChatCrypto is just a
    # method-holder, so a single shared instance serves both sides.
    return ChatCrypto(alice_keys[0], "")


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


def test_seal_open_roundtrip_between_two_managers(crypto, tmp_path):
    a_kp, b_kp = hybrid_keypair(), hybrid_keypair()
    alice = _manager(crypto, tmp_path, me="alice", my_kp=a_kp, peer_pubs={"bob": b_kp.public_key})
    bob = _manager(crypto, tmp_path, me="bob", my_kp=b_kp, peer_pubs={"alice": a_kp.public_key})

    msg = ChatMessage(sender="alice", recipient="bob", content="hello over the ratchet")
    sealed = alice.seal(msg)
    assert sealed.encrypted and alice.can_open(sealed)
    assert sealed.content != "hello over the ratchet"

    opened = bob.open(sealed)
    assert opened.content == "hello over the ratchet"


def test_can_ratchet_false_without_peer_prekey(crypto, tmp_path):
    a_kp = hybrid_keypair()
    alice = _manager(crypto, tmp_path, me="alice", my_kp=a_kp, peer_pubs={})  # no peer prekeys
    assert alice.can_ratchet("bob") is False


def test_open_passes_through_non_ratchet_message(crypto, tmp_path):
    a_kp = hybrid_keypair()
    alice = _manager(crypto, tmp_path, me="alice", my_kp=a_kp, peer_pubs={})
    plain = ChatMessage(sender="bob", recipient="alice", content="classical body")
    assert alice.can_open(plain) is False
    assert alice.open(plain) is plain  # untouched — caller handles classical/hybrid


def test_session_persists_across_manager_restart(crypto, tmp_path):
    a_kp, b_kp = hybrid_keypair(), hybrid_keypair()
    alice = _manager(crypto, tmp_path, me="alice", my_kp=a_kp, peer_pubs={"bob": b_kp.public_key})

    m0 = alice.seal(ChatMessage(sender="alice", recipient="bob", content="m0"))
    # Rebuild Alice's manager from the SAME store dir (daemon restart).
    alice2 = _manager(crypto, tmp_path, me="alice", my_kp=a_kp, peer_pubs={"bob": b_kp.public_key})
    m1 = alice2.seal(ChatMessage(sender="alice", recipient="bob", content="m1"))

    from skchat.dm_session import SealedDmFrame

    f0 = SealedDmFrame.from_token(m0.content)
    f1 = SealedDmFrame.from_token(m1.content)
    assert (f0.epoch, f0.index) == (0, 0)
    assert (f1.epoch, f1.index) == (0, 1)  # continued — no index reuse after restart


def test_for_agent_factory_roundtrip_via_real_prekeys(crypto, tmp_path, monkeypatch):
    """The real wiring: for_agent() resolves peers through the live pq_prekeys store
    and the agent's own persisted hybrid keypair."""
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path / "home"))
    from skchat import pq_prekeys as pk

    if not pk.available():
        pytest.skip("no PQ backend (liboqs) available")

    pk.ensure_agent_keypair("alice")
    pk.ensure_agent_keypair("bob")
    pk.store_peer_bundle("bob", pk.agent_bundle("bob"))  # cross-publish prekeys
    pk.store_peer_bundle("alice", pk.agent_bundle("alice"))

    alice = DmRatchetManager.for_agent(crypto, "alice", tmp_path / "a")
    bob = DmRatchetManager.for_agent(crypto, "bob", tmp_path / "b")
    assert alice is not None and bob is not None
    assert alice.can_ratchet("bob") is True  # resolved bob's real prekey

    sealed = alice.seal(ChatMessage(sender="alice", recipient="bob", content="real wiring"))
    assert ChatCrypto.is_ratchet_message(sealed)  # actually ratchet-sealed
    assert bob.open(sealed).content == "real wiring"


def test_for_agent_skips_peer_without_ratchet_capability(crypto, tmp_path, monkeypatch):
    """SAFETY: a peer with a hybrid prekey but NO pqdr1 capability (your app / an
    older client) must NOT be ratcheted — else it gets unreadable frames."""
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path / "home"))
    from skchat import pq_prekeys as pk

    if not pk.available():
        pytest.skip("no PQ backend (liboqs) available")

    pk.ensure_agent_keypair("alice")
    pk.ensure_agent_keypair("legacy")
    legacy = pk.agent_bundle("legacy")  # a real hybrid bundle...
    legacy.pop("ratchet", None)  # ...but it does NOT advertise pqdr1 support
    pk.store_peer_bundle("legacy", legacy)

    mgr = DmRatchetManager.for_agent(crypto, "alice", tmp_path / "a")
    assert mgr.can_ratchet("legacy") is False  # capability-gated → classical fallback
