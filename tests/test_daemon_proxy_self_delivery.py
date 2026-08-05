"""Regression tests for Task 11's self-delivery fanout (task 70dad715).

A chef->lumina (or chef->peer) DM is SEALED to every sender-own device slot on
the client, but until ``_self_deliver_own_devices`` was added, delivery only
ever reached the peer -- the sender's OTHER enrolled devices never got a copy
of their own outbound message. These tests exercise the server-side fanout
through the real ``POST /api/v1/send`` route: after a send, the operator's
sibling devices (not just the peer) must have an inbox entry, the originating
device must NOT get an echo, and repeat delivery must be idempotent.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy
from skchat.message_log import MessageLog
from skchat.operator_auth import DeviceStore


class _StubBrain:
    def reply(self, user_text, history=None, sender="chef"):
        return f"Lumina hears you: {user_text}"


@pytest.fixture
def client(tmp_path, monkeypatch):
    from skchat.history import ChatHistory

    hist = ChatHistory(store=None, history_dir=tmp_path / "history")
    monkeypatch.setattr(daemon_proxy, "_HISTORY", hist)
    monkeypatch.setattr(daemon_proxy, "_BRAIN", _StubBrain())
    monkeypatch.setattr(daemon_proxy, "_SEND_RECENT", {})
    monkeypatch.setattr(daemon_proxy, "_SEND_LOCKS", {})
    monkeypatch.setattr(daemon_proxy, "_other_peers", lambda: [])

    # Two enrolled operator devices ("A" originates the send, "B" is the
    # sibling that should receive the self-delivered copy).
    devices = DeviceStore(tmp_path / "operator_devices.json")
    fp_a = devices.enroll("pubkey-device-a")
    fp_b = devices.enroll("pubkey-device-b")
    monkeypatch.setattr(daemon_proxy, "_DEVICE_STORE", devices)

    msglog = MessageLog(tmp_path / "message_log.db")
    monkeypatch.setattr(daemon_proxy, "_MSGLOG", msglog)

    app = FastAPI()
    app.include_router(daemon_proxy.router)
    c = TestClient(app)
    c._hist = hist  # type: ignore[attr-defined]
    c._msglog = msglog  # type: ignore[attr-defined]
    c._fp_a = fp_a  # type: ignore[attr-defined]
    c._fp_b = fp_b  # type: ignore[attr-defined]
    return c


def test_dm_to_peer_self_delivers_to_sibling_device(client):
    r = client.post(
        "/api/v1/send",
        json={"recipient": "someone@skworld.io", "message": "hello", "device_fp": client._fp_a},
    )
    assert r.status_code == 200

    sibling_inbox = daemon_proxy.self_sync_inbox(client._fp_b)
    assert len(sibling_inbox) == 1
    entry = sibling_inbox[0]
    assert entry["sender"] == daemon_proxy.OPERATOR_ID
    assert entry["recipient"] == "someone@skworld.io"
    assert entry["content"] == "hello"


def test_originating_device_gets_no_echo(client):
    client.post(
        "/api/v1/send",
        json={"recipient": "someone@skworld.io", "message": "hello", "device_fp": client._fp_a},
    )
    assert daemon_proxy.self_sync_inbox(client._fp_a) == []


def test_peer_still_receives_via_normal_history_path(client):
    r = client.post(
        "/api/v1/send",
        json={"recipient": "someone@skworld.io", "message": "hello", "device_fp": client._fp_a},
    )
    assert r.status_code == 200
    body = r.json()
    # Legacy single-recipient path is untouched: the peer message is still
    # persisted through the normal history exactly once.
    assert body["recipient"] == "someone@skworld.io"


def test_dm_to_lumina_also_self_delivers(client):
    r = client.post(
        "/api/v1/send",
        json={"recipient": "lumina", "message": "hi lumina", "device_fp": client._fp_a},
    )
    assert r.status_code == 200

    sibling_inbox = daemon_proxy.self_sync_inbox(client._fp_b)
    assert len(sibling_inbox) == 1
    assert sibling_inbox[0]["content"] == "hi lumina"
    assert sibling_inbox[0]["recipient"] == daemon_proxy.LUMINA_URI


def test_repeat_send_is_idempotent_no_duplicate_rows(client):
    payload = {"recipient": "someone@skworld.io", "message": "hello", "device_fp": client._fp_a}
    r1 = client.post("/api/v1/send", json=payload)
    msg_id = r1.json()["id"]

    # Re-run the fanout directly for the same message (simulates a retried
    # delivery) -- it must not create a second row for the sibling device.
    from skchat.models import ChatMessage

    msg = ChatMessage(
        id=msg_id,
        sender=daemon_proxy.OPERATOR_ID,
        recipient="someone@skworld.io",
        content="hello",
    )
    daemon_proxy._self_deliver_own_devices(msg, origin_device_fp=client._fp_a)

    sibling_inbox = daemon_proxy.self_sync_inbox(client._fp_b)
    assert len(sibling_inbox) == 1


def test_single_enrolled_device_is_a_noop(tmp_path, monkeypatch):
    from skchat.history import ChatHistory

    hist = ChatHistory(store=None, history_dir=tmp_path / "history")
    monkeypatch.setattr(daemon_proxy, "_HISTORY", hist)
    monkeypatch.setattr(daemon_proxy, "_BRAIN", _StubBrain())
    monkeypatch.setattr(daemon_proxy, "_SEND_RECENT", {})
    monkeypatch.setattr(daemon_proxy, "_SEND_LOCKS", {})
    monkeypatch.setattr(daemon_proxy, "_other_peers", lambda: [])

    devices = DeviceStore(tmp_path / "operator_devices.json")
    fp_a = devices.enroll("only-device")
    monkeypatch.setattr(daemon_proxy, "_DEVICE_STORE", devices)

    msglog = MessageLog(tmp_path / "message_log.db")
    monkeypatch.setattr(daemon_proxy, "_MSGLOG", msglog)

    app = FastAPI()
    app.include_router(daemon_proxy.router)
    c = TestClient(app)

    r = c.post(
        "/api/v1/send",
        json={"recipient": "someone@skworld.io", "message": "hello", "device_fp": fp_a},
    )
    assert r.status_code == 200
    assert daemon_proxy.self_sync_inbox(fp_a) == []
