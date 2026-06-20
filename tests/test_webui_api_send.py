"""Tests for the JSON send route: POST /api/send.

Native-client (Flutter) contract:
  * POST /api/send  body {"recipient", "content"} -> {"ok", "id", "recipient", "ts"}
  * It must run the SAME send_and_store transport path as the HTML /send route.
  * It must broadcast {"type": "new"} to /ws/chat so web clients refresh.
  * Blank content -> 400; missing recipient -> 422.

The legacy HTML POST /send (form -> HTML) is intentionally left unchanged.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from skchat import webui


def _client() -> TestClient:
    return TestClient(webui.app)


class _StubTransport:
    """Captures send_and_store calls so we can assert the path was taken."""

    def __init__(self):
        self.calls: list[dict] = []

    def send_and_store(self, recipient: str, content: str, **kw):
        self.calls.append({"recipient": recipient, "content": content})
        return {"delivered": True, "message_id": "x", "recipient": recipient}


def _patch(monkeypatch, transport):
    monkeypatch.setattr(webui, "_get_identity", lambda: "capauth:tester@local")
    monkeypatch.setattr(webui, "_get_transport", lambda identity: transport)
    broadcasts: list[dict] = []

    async def _fake_broadcast(msg_dict):
        broadcasts.append(msg_dict)

    monkeypatch.setattr(webui, "_ws_broadcast", _fake_broadcast)
    return broadcasts


def test_api_send_uses_transport_and_returns_contract(monkeypatch):
    transport = _StubTransport()
    broadcasts = _patch(monkeypatch, transport)

    r = _client().post(
        "/api/send",
        json={"recipient": "capauth:bob@local", "content": "hello world"},
    )
    assert r.status_code == 200
    body = r.json()
    # Exact JSON contract the Flutter app consumes.
    assert set(body.keys()) == {"ok", "id", "recipient", "ts"}
    assert body["ok"] is True
    assert body["recipient"] == "capauth:bob@local"
    assert isinstance(body["id"], str) and body["id"]
    assert isinstance(body["ts"], str) and "T" in body["ts"]

    # Same send_and_store transport path as the HTML /send route.
    assert transport.calls == [
        {"recipient": "capauth:bob@local", "content": "hello world"}
    ]
    # Web clients are told to refresh, identical to the HTML route.
    assert broadcasts == [{"type": "new"}]


def test_api_send_blank_content_is_400(monkeypatch):
    transport = _StubTransport()
    _patch(monkeypatch, transport)
    r = _client().post(
        "/api/send", json={"recipient": "capauth:bob@local", "content": "   "}
    )
    assert r.status_code == 400
    assert transport.calls == []


def test_api_send_missing_recipient_is_422(monkeypatch):
    transport = _StubTransport()
    _patch(monkeypatch, transport)
    r = _client().post("/api/send", json={"content": "hi"})
    assert r.status_code == 422
    assert transport.calls == []


def test_api_send_falls_back_to_history_when_no_transport(monkeypatch):
    saved: list = []

    class _StubHistory:
        def save(self, msg):
            saved.append(msg)

    monkeypatch.setattr(webui, "_get_identity", lambda: "capauth:tester@local")
    monkeypatch.setattr(webui, "_get_transport", lambda identity: None)
    monkeypatch.setattr(webui, "_get_history", lambda: _StubHistory())

    async def _fake_broadcast(msg_dict):
        return None

    monkeypatch.setattr(webui, "_ws_broadcast", _fake_broadcast)

    r = _client().post(
        "/api/send", json={"recipient": "capauth:bob@local", "content": "hey"}
    )
    assert r.status_code == 200
    assert len(saved) == 1
    assert saved[0].recipient == "capauth:bob@local"
    assert saved[0].content == "hey"
    # The stored message's id/ts match the JSON response.
    body = r.json()
    assert body["id"] == saved[0].id
