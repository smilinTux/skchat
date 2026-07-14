"""Cross-daemon group key distribution over the REAL file transport.

The in-process end-to-end test (test_group_key_delivery) proves the crypto +
consume logic with a fake deliver callback. This proves the piece that only shows
up between two daemons: the key package surviving the actual
``local_deliver_to_agent`` -> ``<agent>/comms/inbox/*.skc.json`` ->
``MessageEnvelope`` -> ``ChatMessage.model_validate_json`` serialization the
receiving daemon's ``poll_inbox`` performs, then being consumed + applied so the
receiver can unseal what the sender sealed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skchat import daemon_proxy_groups as G
from skchat import pq_prekeys as PQ
from skchat.group import GroupChat, MemberRole


def test_key_package_survives_chatmessage_json_roundtrip():
    """The nested group_key_package dict survives ChatMessage -> JSON -> ChatMessage
    (what the file transport does end to end)."""
    from skchat.models import ChatMessage

    pkg = {"type": "group_epoch_advance", "group_id": "g1", "epoch": 1,
           "key_version": 2, "kem_suite": "x25519-mlkem768",
           "distributions": {"capauth:recv@skworld.io": "deadbeef"}}
    msg = ChatMessage(sender="capauth:s@skworld.io", recipient="capauth:recv@skworld.io",
                      content="", metadata={"group_key_package": pkg})
    back = ChatMessage.model_validate_json(msg.model_dump_json())
    assert back.metadata.get("group_key_package") == pkg
    assert G.consume_group_key_message(back, agent="recv") is not None


def test_cross_daemon_delivery_over_file_transport(tmp_path, monkeypatch):
    """Full cross-store chain over the real file transport: sender delivers the key
    package to the receiver agent's comms inbox; the receiver reconstructs it the
    way poll_inbox does, consumes it, and can then unseal the sealed body."""
    if not PQ.available():
        pytest.skip("PQ KEM backend unavailable")
    from skcomms.models import MessageEnvelope
    from skchat.models import ChatMessage

    # single tmp HOME hosts the receiver's keypair, group store, and comms inbox
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    monkeypatch.setenv("SKAGENT", "recv")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    inbox = tmp_path / ".skcapstone" / "agents" / "recv" / "comms" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)

    recv_uri = "capauth:recv@skworld.io"
    pub, _priv = PQ.ensure_agent_keypair("recv")

    # SENDER: hybrid group, receiver keyed with its real pub; seed + seal.
    sender = GroupChat(name="S", kem_suite="x25519-mlkem768")
    sender.add_member(identity_uri="capauth:s@skworld.io", role=MemberRole.ADMIN)
    sender.add_member(identity_uri=recv_uri, hybrid_kem_public_hex=pub.hex())
    sender.ensure_epoch()
    sealed_body = G._seal_group_content(sender, "cross-daemon over the wire")

    # SENDER distributes the key package over the REAL local file transport.
    delivered = G.distribute_group_epoch(sender, "capauth:s@skworld.io",
                                         G.local_deliver_to_agent)
    assert delivered == [recv_uri]
    files = list(inbox.glob("*.skc.json"))
    assert files, "no envelope landed in the receiver inbox"

    # RECEIVER: a local group copy with no secret yet.
    recv_group = GroupChat(name="S", kem_suite="x25519-mlkem768")
    recv_group.id = sender.id
    recv_group.add_member(identity_uri=recv_uri, role=MemberRole.ADMIN)
    G.save_group(recv_group)

    # RECEIVER reconstructs the ChatMessage exactly as poll_inbox does, consumes it.
    env = MessageEnvelope.from_bytes(files[0].read_bytes())
    msg = ChatMessage.model_validate_json(env.payload.content)
    assert msg.metadata.get("group_key_package")               # survived the envelope
    assert G.consume_group_key_message(msg, agent="recv") is True

    # END TO END across stores: receiver now unseals what the sender sealed.
    keyed = G.load_group(sender.id)
    assert keyed.epoch_secret_hex == sender.epoch_secret_hex
    assert G.unseal_group_content(keyed, sealed_body) == "cross-daemon over the wire"
