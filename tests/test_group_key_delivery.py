"""SEAM 9 delivery wiring — a group's wrapped epoch secret is DELIVERED to member
daemons (build → distribute → consume) so a receiver can unseal sealed group
messages.

The crypto core (``apply_group_key_package`` / ``GroupKeyDistributor``) is already
built + proven (see ``tests/test_group_seal.py``). These tests cover the delivery
layer around it: building the typed ``group_epoch_advance`` package, distributing it
to keyed members as a TYPED control message (``metadata['group_key_package']``, never
a ``__PREFIX__`` in content — honours SEAM 5), firing distribution once-per-epoch from
the sealed send path, and consuming the message on the receiver. All new behaviour is
gated behind ``SKCHAT_SEAL_GROUPS`` (default OFF, no behaviour change when off).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from skchat import daemon_proxy_groups as G
from skchat.group import GroupChat, MemberRole
from skchat.history import ChatHistory


def _hist() -> ChatHistory:
    """A ChatHistory rooted under the test's SKCHAT_HOME tmp dir (no live store)."""
    home = Path(os.environ["SKCHAT_HOME"])
    return ChatHistory(store=None, history_dir=home / "history")


@pytest.fixture
def isolated_groupstore(tmp_path, monkeypatch):
    """Group store + history in a tmp dir (SKCHAT_HOME), no real network delivery."""
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    monkeypatch.setattr(G, "_GROUPS_DIR", tmp_path / "groups")
    return tmp_path


def _hybrid_group_with_member(pub_hex):
    """A hybrid group: an ADMIN sender (classical, keyless for hybrid) + one KEYED
    hybrid member. ``ensure_epoch`` seeds the epoch secret."""
    g = GroupChat(name="Ops", kem_suite="x25519-mlkem768")
    g.add_member(identity_uri="capauth:sender@skworld.io", role=MemberRole.ADMIN)
    g.add_member(identity_uri="capauth:recv@skworld.io", hybrid_kem_public_hex=pub_hex)
    g.ensure_epoch()
    return g


# ── Regression: a typed control message carries NO body (routes by metadata) ───

def test_key_message_with_empty_content_is_constructible():
    """A control-plane key message routes by ``metadata['group_key_package']`` and
    carries NO chat body — the model must accept an empty ``content`` when the
    typed package is present (else ``distribute_group_epoch`` raises on build and
    the whole delivery layer silently fails). See models._require_content_or_...."""
    from skchat.models import ChatMessage
    m = ChatMessage(sender="capauth:s@skworld.io", recipient="capauth:recv@skworld.io",
                    content="", thread_id="gid",
                    metadata={"group_key_package": {"type": "group_epoch_advance"}})
    assert m.content == "" and m.metadata["group_key_package"]["type"] == "group_epoch_advance"


# ── Task 1: build + distribute the epoch package ───────────────────────────────

def test_build_package_shape():
    g = _hybrid_group_with_member("aa" * 32)
    pkg = G.build_group_epoch_package(g)
    assert pkg["type"] == "group_epoch_advance"
    assert pkg["group_id"] == g.id and pkg["epoch"] == g.epoch
    assert pkg["key_version"] == g.key_version and pkg["kem_suite"] == g.kem_suite
    assert "capauth:recv@skworld.io" in pkg["distributions"]


def test_distribute_delivers_typed_message_to_keyed_members_only():
    g = _hybrid_group_with_member("bb" * 32)
    sent = []

    def deliver(m):
        sent.append(m)
        return True

    delivered = G.distribute_group_epoch(g, "capauth:sender@skworld.io", deliver)
    assert delivered == ["capauth:recv@skworld.io"]        # sender excluded, keyless excluded
    m = sent[0]
    assert m.recipient == "capauth:recv@skworld.io"
    assert m.metadata.get("group_key_package", {}).get("type") == "group_epoch_advance"
    assert m.metadata.get("group_id") == g.id
    # Control-plane routing is TYPED, never a __PREFIX__ in content (SEAM 5).
    assert m.content == ""


def test_distribute_deliver_failure_is_fail_closed_readable():
    """A raising ``deliver`` never crashes distribution; the member is just not
    counted as delivered."""
    g = _hybrid_group_with_member("dd" * 32)

    def deliver(m):
        raise RuntimeError("transport down")

    delivered = G.distribute_group_epoch(g, "capauth:sender@skworld.io", deliver)
    assert delivered == []


# ── Task 2: fire distribution from the send path, once per epoch ───────────────

def test_fan_out_distributes_epoch_once_when_sealing(isolated_groupstore, monkeypatch):
    monkeypatch.setenv("SKCHAT_SEAL_GROUPS", "1")
    delivered = []
    monkeypatch.setattr(G, "local_deliver_to_agent",
                        lambda m: (delivered.append(m) or True))
    monkeypatch.setattr(G, "_delivery_transport", lambda uri: None)
    g = _hybrid_group_with_member("cc" * 32)
    G.save_group(g)
    G.fan_out_send(g, _hist(), "capauth:sender@skworld.io", "one")
    key_msgs = [m for m in delivered if m.metadata.get("group_key_package")]
    assert len(key_msgs) == 1                                  # distributed once
    G.fan_out_send(G.load_group(g.id), _hist(), "capauth:sender@skworld.io", "two")
    key_msgs = [m for m in delivered if m.metadata.get("group_key_package")]
    assert len(key_msgs) == 1                                  # NOT re-distributed


def test_fan_out_does_not_distribute_when_flag_off(isolated_groupstore, monkeypatch):
    monkeypatch.delenv("SKCHAT_SEAL_GROUPS", raising=False)
    delivered = []
    monkeypatch.setattr(G, "local_deliver_to_agent",
                        lambda m: (delivered.append(m) or True))
    monkeypatch.setattr(G, "_delivery_transport", lambda uri: None)
    g = _hybrid_group_with_member("ce" * 32)
    G.save_group(g)
    G.fan_out_send(g, _hist(), "capauth:sender@skworld.io", "one")
    key_msgs = [m for m in delivered if m.metadata.get("group_key_package")]
    assert key_msgs == []                                      # nothing distributed


def test_fan_out_key_delivery_count_is_honest_on_failure(isolated_groupstore, monkeypatch):
    """The epoch-key delivery reports the ACTUAL outcome, not a blanket ``or True``:
    when neither the local inbox nor a network transport delivers, NO member is
    counted delivered (a dropped key copy is never silently recorded), yet the chat
    send still proceeds and the epoch is marked distributed (attempt once, never
    re-loop the send). Locks in the fail-closed-readable delivery-count contract."""
    monkeypatch.setenv("SKCHAT_SEAL_GROUPS", "1")
    monkeypatch.setattr(G, "local_deliver_to_agent", lambda m: False)   # local delivery fails
    monkeypatch.setattr(G, "_delivery_transport", lambda uri: None)      # no network transport
    recorded = {}
    real_distribute = G.distribute_group_epoch

    def spy(group, sender_uri, deliver):
        out = real_distribute(group, sender_uri, deliver)
        recorded["delivered"] = out
        return out

    monkeypatch.setattr(G, "distribute_group_epoch", spy)
    g = _hybrid_group_with_member("cf" * 32)
    G.save_group(g)
    gmsg = G.fan_out_send(g, _hist(), "capauth:sender@skworld.io", "hi")
    assert recorded["delivered"] == []                          # honest: nothing delivered
    assert gmsg.content == "hi"                                 # send still proceeds
    assert G.load_group(g.id).metadata.get("epoch_distributed") == g.epoch  # attempted once


# ── Task 3: receiver daemon consumes the key message ───────────────────────────

def test_consume_group_key_message_keys_local_group(tmp_path, monkeypatch):
    from skchat import pq_prekeys as PQ
    if not PQ.available():
        pytest.skip("no PQ")
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    monkeypatch.setenv("SKAGENT", "recv")
    from skchat.models import ChatMessage
    pub, _ = PQ.ensure_agent_keypair("recv")
    recv_uri = "capauth:recv@skworld.io"
    sender = GroupChat(name="S", kem_suite="x25519-mlkem768")
    sender.add_member(identity_uri="capauth:s@skworld.io", role=MemberRole.ADMIN)
    sender.add_member(identity_uri=recv_uri, hybrid_kem_public_hex=pub.hex())
    sender.ensure_epoch()
    pkg = G.build_group_epoch_package(sender)
    recv = GroupChat(name="S", kem_suite="x25519-mlkem768")
    recv.id = sender.id
    recv.add_member(identity_uri=recv_uri, role=MemberRole.ADMIN)
    G.save_group(recv)
    msg = ChatMessage(sender="capauth:s@skworld.io", recipient=recv_uri, content="",
                      thread_id=sender.id, metadata={"group_key_package": pkg})
    assert G.consume_group_key_message(msg, agent="recv") is True
    assert G.load_group(sender.id).epoch_secret_hex == sender.epoch_secret_hex
    # a normal message is not consumed
    assert G.consume_group_key_message(
        ChatMessage(sender="x", recipient=recv_uri, content="hi"), agent="recv") is False


def test_consume_routes_by_typed_metadata_without_pq(tmp_path, monkeypatch):
    """The poll-loop seam is CONTROL-PLANE routing by typed metadata, independent
    of the KEM backend: a message carrying a ``group_key_package`` is CONSUMED
    (True) even when the unwrap can't complete (apply fails closed — no local group
    / no distribution for us), and a normal chat message is NOT consumed (False).
    Exercises the delivery seam + fail-closed apply branch in CI without needing a
    live PQ KEM (the PQ round-trip tests self-skip when liboqs is absent)."""
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    from skchat.models import ChatMessage
    # Typed control message: apply fails closed (empty distributions), yet the
    # message is still consumed so it never becomes a chat turn.
    key_msg = ChatMessage(
        sender="capauth:s@skworld.io", recipient="capauth:recv@skworld.io",
        content="", thread_id="gid",
        metadata={"group_key_package": {"type": "group_epoch_advance",
                                        "group_id": "gid", "distributions": {}}})
    assert G.consume_group_key_message(key_msg, agent="recv") is True
    # A normal chat message is not consumed (routes onward to chat handling).
    assert G.consume_group_key_message(
        ChatMessage(sender="a", recipient="capauth:recv@skworld.io", content="hi"),
        agent="recv") is False
    # A message with a non-dict package is likewise not consumed.
    assert G.consume_group_key_message(
        ChatMessage(sender="a", recipient="capauth:recv@skworld.io", content="yo",
                    metadata={"group_key_package": "not-a-dict"})) is False


# ── Task 4: end-to-end delivery integration ────────────────────────────────────

def test_send_distribute_consume_unseal_end_to_end(tmp_path, monkeypatch):
    from skchat import pq_prekeys as PQ
    if not PQ.available():
        pytest.skip("no PQ")
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    monkeypatch.setenv("SKAGENT", "recv")
    pub, _ = PQ.ensure_agent_keypair("recv")
    recv_uri = "capauth:recv@skworld.io"
    inbox = []
    monkeypatch.setenv("SKCHAT_SEAL_GROUPS", "1")
    monkeypatch.setattr(G, "local_deliver_to_agent", lambda m: (inbox.append(m) or True))
    monkeypatch.setattr(G, "_delivery_transport", lambda uri: None)
    g = GroupChat(name="S", kem_suite="x25519-mlkem768")
    g.add_member(identity_uri="capauth:s@skworld.io", role=MemberRole.ADMIN)
    g.add_member(identity_uri=recv_uri, hybrid_kem_public_hex=pub.hex())
    g.ensure_epoch()
    G.save_group(g)
    # receiver's own local group copy (no secret yet)
    r = GroupChat(name="S", kem_suite="x25519-mlkem768")
    r.id = g.id
    r.add_member(identity_uri=recv_uri, role=MemberRole.ADMIN)
    G.save_group(r)
    G.save_group(g)  # restore sender copy as canonical for load in the send
    G.fan_out_send(G.load_group(g.id), _hist(), "capauth:s@skworld.io", "e2e secret")
    key_msgs = [m for m in inbox if m.metadata.get("group_key_package")]
    sealed_msgs = [m for m in inbox if G.is_sealed_group_content(getattr(m, "content", ""))]
    assert key_msgs and sealed_msgs
    for m in key_msgs:                      # receiver consumes the key
        G.consume_group_key_message(m, agent="recv")
    opened = G.unseal_group_content(G.load_group(g.id), sealed_msgs[0].content)
    assert opened == "e2e secret"
