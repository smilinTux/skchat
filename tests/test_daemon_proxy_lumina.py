"""Tests for the Lumina chat surface in ``skchat.daemon_proxy``.

Contract the Flutter app depends on:
  * Lumina is ALWAYS present + first in /api/v1/peers and /api/v1/conversations,
    flagged ``is_agent`` and ``is_online`` while the operator is in-app.
  * POST /api/v1/send to Lumina persists the operator's message, invokes her
    brain (mocked offline here), persists + returns her reply.
  * The thread reads back through /api/v1/conversations + /api/v1/inbox +
    /api/v1/conversations/{peer_id}, ordered oldest-first with timestamps.

The qwen3.6 HTTP backend is never touched — a stub ``LuminaBrain`` is injected.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy


class _StubBrain:
    """Offline stand-in for the real LuminaBrain.reply (no qwen HTTP)."""

    def __init__(self):
        self.calls: list[dict] = []

    def reply(self, user_text, history=None, sender="chef"):
        self.calls.append({"text": user_text, "history": history or []})
        return f"Lumina hears you: {user_text}"


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Isolate history to a tmp JSONL dir.
    from skchat.history import ChatHistory

    hist = ChatHistory(store=None, history_dir=tmp_path / "history")
    monkeypatch.setattr(daemon_proxy, "_HISTORY", hist)

    # Inject the offline brain so no backend call ever happens.
    brain = _StubBrain()
    monkeypatch.setattr(daemon_proxy, "_BRAIN", brain)

    # Don't enrich with the operator's real ~/.skcapstone/peers in tests.
    monkeypatch.setattr(daemon_proxy, "_other_peers", lambda: [])

    app = FastAPI()
    app.include_router(daemon_proxy.router)
    c = TestClient(app)
    c._brain = brain  # type: ignore[attr-defined]
    return c


def test_lumina_always_present_in_peers_and_conversations(client):
    for path in ("/api/v1/peers", "/api/v1/conversations"):
        r = client.get(path)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list) and body, f"{path} empty"
        first = body[0]
        assert first["peer_id"] == daemon_proxy.LUMINA_ID
        assert first["display_name"] == "Lumina"
        assert first["is_agent"] is True
        assert first["is_online"] is True
        # Full app conversation contract present.
        for key in ("last_message", "last_message_time", "soul_fingerprint",
                    "unread_count", "is_group", "member_count", "avatar_url"):
            assert key in first


def test_send_to_lumina_persists_pair_and_returns_reply(client):
    r = client.post("/api/v1/send", json={"recipient": "lumina", "message": "hi there"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["recipient"] == daemon_proxy.LUMINA_ID
    # Her reply is returned, non-empty, in her voice (stub), flagged agent.
    reply = body["reply"]
    assert reply["content"] == "Lumina hears you: hi there"
    assert reply["is_agent"] is True
    assert reply["is_outbound"] is False
    assert reply["reply_to_id"] == body["id"]
    # The brain was actually invoked.
    assert client._brain.calls and client._brain.calls[0]["text"] == "hi there"

    # Thread reads back: the operator message + Lumina's reply, oldest-first.
    hist = client.get("/api/v1/conversations/" + daemon_proxy.LUMINA_ID).json()
    assert [m["content"] for m in hist] == ["hi there", "Lumina hears you: hi there"]
    assert hist[0]["is_outbound"] is True and hist[0]["is_agent"] is False
    assert hist[1]["is_outbound"] is False and hist[1]["is_agent"] is True
    assert all("timestamp" in m for m in hist)

    # Inbox carries the same thread (app groups by peer_id).
    inbox = client.get("/api/v1/inbox").json()["messages"]
    assert [m["content"] for m in inbox] == ["hi there", "Lumina hears you: hi there"]
    assert all(m["peer_id"] == daemon_proxy.LUMINA_ID for m in inbox)

    # Conversation list preview reflects the latest message.
    convo = client.get("/api/v1/conversations").json()[0]
    assert convo["last_message"] == "Lumina hears you: hi there"


def test_send_empty_message_is_400(client):
    r = client.post("/api/v1/send", json={"recipient": "lumina", "message": "   "})
    assert r.status_code == 400


def test_brain_failure_persists_graceful_reply_not_500(client, monkeypatch):
    class _BoomBrain:
        def reply(self, *a, **k):
            raise RuntimeError("backend down")

    monkeypatch.setattr(daemon_proxy, "_BRAIN", _BoomBrain())
    r = client.post("/api/v1/send", json={"recipient": "lumina", "message": "you there?"})
    assert r.status_code == 200
    reply = r.json()["reply"]
    assert reply["content"] and "thinking failed" in reply["content"].lower()
    # Both turns are still persisted.
    hist = client.get("/api/v1/conversations/" + daemon_proxy.LUMINA_ID).json()
    assert len(hist) == 2 and hist[0]["content"] == "you there?"
