"""Cross-daemon group key distribution over the REAL file transport.

``test_group_key_delivery.py`` proves the build → distribute → consume → unseal
chain IN-PROCESS: the sender and receiver share ONE group store, and delivery is
monkeypatched to an in-memory list. This restores the true multi-daemon /
cross-store leg the delivery plan flagged as a follow-up
(``docs/superpowers/plans/2026-07-14-group-key-distribution-delivery.md`` §Step 1
note: "the true multi-daemon cross-store test is a follow-up runbook step").

Here the sender and receiver each hold their OWN group store + their OWN hybrid
keypair (separate ``SKCHAT_HOME``), and the epoch-key package + the sealed message
ride the REAL on-disk file-transport envelope: ``local_deliver_to_agent`` (NOT
monkeypatched) drops a ``.skc.json`` :class:`skcomms.models.MessageEnvelope` into
the receiver agent's ``~/.skcapstone/agents/<agent>/comms/inbox/``, which we read
back with the SAME ``MessageEnvelope -> payload.content -> ChatMessage`` decode the
daemon's ``ChatTransport.poll_inbox`` uses. So the wire format is exercised end to
end, not skipped. Only the network fallback (never taken once local delivery
succeeds) is stubbed to keep the test hermetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skchat import daemon_proxy_groups as G
from skchat import pq_prekeys as PQ
from skchat.group import GroupChat, MemberRole
from skchat.history import ChatHistory


SENDER_URI = "capauth:sender@skworld.io"
RECV_URI = "capauth:recv@skworld.io"


def _hist(home: Path) -> ChatHistory:
    """A ChatHistory rooted under *home* (no live ~/.skchat store touched)."""
    return ChatHistory(store=None, history_dir=home / "history")


def _recv_inbox(home: Path) -> Path:
    """The receiver agent's comms inbox that ``local_deliver_to_agent`` writes to."""
    return home / ".skcapstone" / "agents" / "recv" / "comms" / "inbox"


def _read_inbox_messages(inbox: Path):
    """Decode every ``.skc.json`` envelope in *inbox* back to a ChatMessage.

    This is the SAME path the daemon's poll takes: parse the on-disk
    :class:`MessageEnvelope`, pull ``payload.content`` (the inner ChatMessage JSON),
    and validate it into a :class:`ChatMessage`.
    """
    from skcomms.models import MessageEnvelope

    from skchat.models import ChatMessage

    out = []
    for f in sorted(inbox.glob("*.skc.json")):
        env = MessageEnvelope.model_validate_json(f.read_text(encoding="utf-8"))
        out.append(ChatMessage.model_validate_json(env.payload.content))
    return out


def test_cross_store_key_package_survives_real_file_transport(tmp_path, monkeypatch):
    """A typed epoch-key control message round-trips through the REAL on-disk
    file-transport envelope and is still routed as control-plane (consumed), with
    NO PQ backend required — locks in that the wire format preserves
    ``metadata['group_key_package']`` across a genuine daemon-to-daemon hop."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path / "recv"))
    _recv_inbox(tmp_path).mkdir(parents=True, exist_ok=True)

    from skchat.models import ChatMessage

    key_msg = ChatMessage(
        sender=SENDER_URI, recipient=RECV_URI, content="", thread_id="gid",
        metadata={"group_key_package": {"type": "group_epoch_advance",
                                        "group_id": "gid", "distributions": {}}})
    # REAL delivery: drops an actual .skc.json envelope into the receiver inbox.
    assert G.local_deliver_to_agent(key_msg) is True

    received = _read_inbox_messages(_recv_inbox(tmp_path))
    assert len(received) == 1
    got = received[0]
    # The typed package survived the MessageEnvelope round-trip on disk.
    assert got.metadata.get("group_key_package", {}).get("type") == "group_epoch_advance"
    # Receiver routes it as control-plane and consumes it (apply fails closed —
    # empty distributions / no local group — but it is never a chat turn).
    assert G.consume_group_key_message(got, agent="recv") is True


def test_cross_store_send_distribute_consume_unseal_over_file_transport(tmp_path, monkeypatch):
    """Full multi-daemon chain across SEPARATE stores over the real file transport:

    sender store seals + distributes the wrapped epoch secret -> the package and
    the sealed body land as real ``.skc.json`` envelopes in the receiver's inbox ->
    the receiver store CONSUMES the key (keying its own group copy) -> the receiver
    UNSEALS the sender's sealed body to the original plaintext.
    """
    if not PQ.available():
        pytest.skip("PQ KEM backend unavailable")

    sender_home = tmp_path / "sender"
    recv_home = tmp_path / "recv"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SKCHAT_SEAL_GROUPS", "1")
    # Real local file-transport delivery; only the network fallback is stubbed
    # (never taken once the local drop succeeds).
    monkeypatch.setattr(G, "_delivery_transport", lambda uri: None)
    _recv_inbox(tmp_path).mkdir(parents=True, exist_ok=True)

    # ── Receiver store: mint the receiver's OWN hybrid keypair (its private key
    # lives ONLY here, under recv's SKCHAT_HOME) and its keyless group copy. ──
    monkeypatch.setenv("SKCHAT_HOME", str(recv_home))
    monkeypatch.setenv("SKAGENT", "recv")
    pub, _priv = PQ.ensure_agent_keypair("recv")

    # ── Sender store: hybrid group; receiver is a keyed member; seed the epoch. ──
    monkeypatch.setenv("SKCHAT_HOME", str(sender_home))
    sender = GroupChat(name="Secure", kem_suite="x25519-mlkem768")
    sender.add_member(identity_uri=SENDER_URI, role=MemberRole.ADMIN)
    sender.add_member(identity_uri=RECV_URI, hybrid_kem_public_hex=pub.hex())
    sender.ensure_epoch()
    assert sender.is_hybrid and sender.epoch_secret_hex
    G.save_group(sender)
    gid = sender.id

    # Receiver's own local copy of the group (same id, NO epoch secret yet).
    monkeypatch.setenv("SKCHAT_HOME", str(recv_home))
    recv = GroupChat(name="Secure", kem_suite="x25519-mlkem768")
    recv.id = gid
    recv.add_member(identity_uri=RECV_URI, role=MemberRole.ADMIN)
    G.save_group(recv)
    assert not G.load_group(gid).epoch_secret_hex

    # ── Sender store: fan out. This seals the body, distributes the epoch key,
    # and drops BOTH as real .skc.json envelopes into the receiver's inbox. ──
    monkeypatch.setenv("SKCHAT_HOME", str(sender_home))
    G.fan_out_send(G.load_group(gid), _hist(sender_home), SENDER_URI, "cross-store secret")

    delivered = _read_inbox_messages(_recv_inbox(tmp_path))
    key_msgs = [m for m in delivered if m.metadata.get("group_key_package")]
    sealed_msgs = [m for m in delivered if G.is_sealed_group_content(m.content)]
    assert key_msgs, "epoch key package was not delivered over the file transport"
    assert sealed_msgs, "sealed body was not delivered over the file transport"

    # ── Receiver store: consume the key (keys the local copy) then unseal. ──
    monkeypatch.setenv("SKCHAT_HOME", str(recv_home))
    for m in key_msgs:
        assert G.consume_group_key_message(m, agent="recv") is True
    recv_keyed = G.load_group(gid)
    assert recv_keyed.epoch_secret_hex == sender.epoch_secret_hex
    assert recv_keyed.epoch == sender.epoch

    # The receiver decrypts what the sender sealed — end to end, cross-store.
    opened = G.unseal_incoming_group_message(sealed_msgs[0])
    assert opened.content == "cross-store secret"
    assert not G.is_sealed_group_content(opened.content)
