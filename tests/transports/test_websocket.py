"""Unit tests for the WebSocket voice/text transport.

Uses FastAPI TestClient (sync WebSocket) with an injected fake VoiceEngine so
no live endpoints are needed.
"""

import json

from fastapi.testclient import TestClient

from skchat.transports.websocket import build_app


class FakeEngine:
    """Fake VoiceEngine that echoes its transcript as a reply."""

    async def respond(
        self,
        transcript: str,
        history: list,
        *,
        mode: str = "sacred",
        speaker_id: str = "",
        is_operator: bool = True,
    ) -> str:
        return f"echo: {transcript}"


def _app(engine=None):
    return build_app(engine_factory=lambda agent: engine or FakeEngine())


def test_text_message_gets_transcript_reply():
    app = _app()
    client = TestClient(app)
    with client.websocket_connect("/ws/voice/lumina") as ws:
        ws.send_text(json.dumps({"type": "text_message", "text": "hello"}))
        # Expect: status thinking, transcript assistant, status speaking, status listening
        msgs = []
        for _ in range(4):
            msgs.append(ws.receive())
        types_seen = [m.get("text") for m in msgs]
        decoded = [json.loads(t) for t in types_seen if t]
        type_list = [d["type"] for d in decoded]
        assert "transcript" in type_list
        transcripts = [d for d in decoded if d["type"] == "transcript"]
        assert any("echo: hello" in t["text"] for t in transcripts)


def test_clear_history_resets_state():
    app = _app()
    client = TestClient(app)
    with client.websocket_connect("/ws/voice/lumina") as ws:
        ws.send_text("CLEAR_HISTORY")
        msg = json.loads(ws.receive()["text"])
        assert msg["type"] == "status"
        assert msg["state"] == "history_cleared"


def test_unknown_json_is_silently_ignored():
    app = _app()
    client = TestClient(app)
    with client.websocket_connect("/ws/voice/lumina") as ws:
        ws.send_text(json.dumps({"type": "unknown_event", "data": "x"}))
        # Server should not crash — send a clear after and confirm it still works
        ws.send_text("CLEAR_HISTORY")
        msg = json.loads(ws.receive()["text"])
        assert msg["state"] == "history_cleared"
