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


def _drain_turn(ws):
    """Read one text turn's frames (thinking, transcript, speaking, listening)."""
    return [ws.receive() for _ in range(4)]


def test_real_engine_builds_persona_once_per_connection_and_never_searches():
    """Chef's rule over the WebSocket transport: one persona build per session,
    no per-turn memory search, refresh only on CLEAR_HISTORY."""
    from skchat.voice_engine.config import VoiceConfig
    from skchat.voice_engine.engine import VoiceEngine

    systems, searches, builds = [], [], []

    class LLM:
        async def reply(self, messages, **kw):
            systems.append(messages[0]["content"])
            return "ok"

    class Mem:
        async def search(self, *a, **k):
            searches.append(a)
            return ""

    class Persona:
        def build(self, agent, *, mode="private"):
            builds.append(mode)
            return f"You are {agent}."

    engines = []

    def factory(agent):
        eng = VoiceEngine(
            VoiceConfig.from_env(env={}),
            agent,
            llm=LLM(),
            memory=Mem(),
            persona=Persona(),
            context_loader=lambda a, b: ("What you remember: test block", {}),
        )
        engines.append(eng)
        return eng

    client = TestClient(build_app(engine_factory=factory))
    with client.websocket_connect("/ws/voice/lumina") as ws:
        for text in ("one", "two"):
            ws.send_text(json.dumps({"type": "text_message", "text": text}))
            _drain_turn(ws)
        assert builds == ["sacred"]
        ws.send_text("CLEAR_HISTORY")
        ws.receive()
        ws.send_text(json.dumps({"type": "text_message", "text": "three"}))
        _drain_turn(ws)

    assert len(engines) == 1
    assert builds == ["sacred", "sacred"]  # once, then once more after the refresh
    assert searches == []
    assert systems[0] == systems[1] == systems[2]
    assert "test block" in systems[0]
