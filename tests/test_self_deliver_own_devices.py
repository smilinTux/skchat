"""Regression tests for Task 11's self-delivery fanout (card 70dad715).

An outbound DM is SEALED (by the app) to every peer device slot AND every
sender-own device slot, but until this fix the daemon only ever DELIVERED
the send to the peer -- the sender's OTHER devices never got a copy of their
own outbound post. ``daemon_proxy._self_deliver_own_devices`` closes that
gap: when the sender has more than one published prekey slot (i.e. a sibling
device exists), the same raw wire body is relayed into the sender's own
local agent inbox (:func:`daemon_proxy_groups.local_deliver_to_agent`), same
mechanism ``test_daemon_proxy_groups.py`` already exercises for cross-agent
delivery.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy
from skchat import pq_prekeys as PQ


class _StubBrain:
    def reply(self, user_text, history=None, sender="chef"):
        return f"Lumina hears you: {user_text}"


@pytest.fixture
def client(tmp_path, monkeypatch):
    from skchat.history import ChatHistory

    monkeypatch.setenv("HOME", str(tmp_path))

    hist = ChatHistory(store=None, history_dir=tmp_path / "history")
    monkeypatch.setattr(daemon_proxy, "_HISTORY", hist)
    monkeypatch.setattr(daemon_proxy, "_BRAIN", _StubBrain())
    monkeypatch.setattr(daemon_proxy, "_SEND_RECENT", {})
    monkeypatch.setattr(daemon_proxy, "_SEND_LOCKS", {})
    monkeypatch.setattr(daemon_proxy, "_other_peers", lambda: [])

    app = FastAPI()
    app.include_router(daemon_proxy.router)
    c = TestClient(app)
    c._hist = hist  # type: ignore[attr-defined]
    c._home = tmp_path  # type: ignore[attr-defined]
    return c


def _chef_inbox(tmp_path):
    inbox = tmp_path / ".skcapstone" / "agents" / "chef" / "comms" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    return inbox


def _sibling_bundles(count):
    """Fake prekey slots -- only ``len()`` matters to the gating logic."""
    return [{"key_id": f"k{i}"} for i in range(count)]


def test_dm_send_self_delivers_to_sender_sibling_device(client, monkeypatch):
    """Sending a DM to a peer ALSO drops a copy in the sender's own inbox
    (the sibling device) when the sender has published more than one prekey
    slot, in addition to the peer receiving the message."""
    inbox = _chef_inbox(client._home)
    monkeypatch.setattr(PQ, "load_peer_bundles", lambda peer: _sibling_bundles(2))

    r = client.post("/api/v1/send", json={"recipient": "bob", "message": "hello bob"})
    assert r.status_code == 200, r.text
    sent_id = r.json()["id"]

    # Criterion 1: the peer's own inbox entry (the pre-existing behavior) --
    # the message is stored addressed to the peer.
    conv = client._hist.get_messages(peer="bob")
    assert any(m["content"] == "hello bob" and m["recipient"] == "bob" for m in conv)

    # Criterion 1/3: the sender's sibling device also gets a copy, delivered
    # via the same local-inbox mechanism used for cross-agent fanout.
    files = list(inbox.glob("*.skc.json"))
    assert len(files) == 1

    from skcomms.models import MessageEnvelope
    from skchat.models import ChatMessage

    env = MessageEnvelope.model_validate_json(files[0].read_text())
    assert env.sender == daemon_proxy.OPERATOR_ID
    assert env.recipient == daemon_proxy.OPERATOR_ID

    inner = ChatMessage.model_validate_json(env.payload.content)
    assert inner.content == "hello bob"
    # Reuses the persisted message's id (idempotency key for the receiving
    # daemon's own dedup) -- criterion 2.
    assert inner.id == sent_id


def test_dm_send_to_lumina_also_self_delivers(client, monkeypatch):
    """The Lumina brain-reply path goes through a separate call site
    (``_self_deliver_own_devices`` is invoked before the brain call); it must
    fan out to the sender's sibling device too."""
    inbox = _chef_inbox(client._home)
    monkeypatch.setattr(PQ, "load_peer_bundles", lambda peer: _sibling_bundles(2))

    r = client.post("/api/v1/send", json={"recipient": "lumina", "message": "hi lumina"})
    assert r.status_code == 200, r.text

    files = list(inbox.glob("*.skc.json"))
    assert len(files) == 1
    from skchat.models import ChatMessage
    from skcomms.models import MessageEnvelope

    env = MessageEnvelope.model_validate_json(files[0].read_text())
    inner = ChatMessage.model_validate_json(env.payload.content)
    assert inner.content == "hi lumina"


def test_single_device_sender_is_a_no_op(client, monkeypatch):
    """No sibling device published (<=1 prekey slot) -> nothing is fanned
    out; the classical single-recipient path is untouched."""
    inbox = _chef_inbox(client._home)
    monkeypatch.setattr(PQ, "load_peer_bundles", lambda peer: _sibling_bundles(1))

    r = client.post("/api/v1/send", json={"recipient": "bob", "message": "solo device"})
    assert r.status_code == 200, r.text

    assert list(inbox.glob("*.skc.json")) == []


def test_no_sibling_bundles_published_is_a_no_op(client, monkeypatch):
    inbox = _chef_inbox(client._home)
    monkeypatch.setattr(PQ, "load_peer_bundles", lambda peer: [])

    r = client.post("/api/v1/send", json={"recipient": "bob", "message": "no devices yet"})
    assert r.status_code == 200, r.text

    assert list(inbox.glob("*.skc.json")) == []


def test_self_delivery_never_echoes_to_originating_device(client, monkeypatch):
    """The self-copy is addressed to the SENDER's own identity, never back to
    ``bob`` (the peer) and never duplicated onto whatever inbox the
    originating device itself reads from -- only ONE fanout copy per send."""
    inbox = _chef_inbox(client._home)
    monkeypatch.setattr(PQ, "load_peer_bundles", lambda peer: _sibling_bundles(3))

    r = client.post("/api/v1/send", json={"recipient": "bob", "message": "no echo please"})
    assert r.status_code == 200, r.text

    files = list(inbox.glob("*.skc.json"))
    assert len(files) == 1  # exactly one sibling-device copy, not one per slot

    bob_inbox = client._home / ".skcapstone" / "agents" / "bob" / "comms" / "inbox"
    assert not bob_inbox.exists() or not list(bob_inbox.glob("*.skc.json"))


def test_self_delivery_is_idempotent_across_resends(client, monkeypatch):
    """A resend of the identical message (e.g. the app's HTTP-timeout retry)
    reuses the same message id in the fanned-out copy both times, so a
    receiving daemon's own dedup collapses them to one -- it never manufactures
    a fresh id per delivery attempt."""
    from skchat.models import ChatMessage

    monkeypatch.setattr(PQ, "load_peer_bundles", lambda peer: _sibling_bundles(2))
    inbox = _chef_inbox(client._home)

    msg = ChatMessage(sender=daemon_proxy.OPERATOR_ID, recipient="bob", content="retry me")
    daemon_proxy._self_deliver_own_devices(msg, "retry me")
    daemon_proxy._self_deliver_own_devices(msg, "retry me")

    files = sorted(inbox.glob("*.skc.json"))
    assert len(files) == 2  # two delivery attempts on disk...

    from skcomms.models import MessageEnvelope

    ids = set()
    for f in files:
        env = MessageEnvelope.model_validate_json(f.read_text())
        inner = ChatMessage.model_validate_json(env.payload.content)
        ids.add(inner.id)
    assert ids == {msg.id}  # ...but they carry the SAME inner id (idempotent).
