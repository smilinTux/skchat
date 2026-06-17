"""E2E wiring test for the in-process voice turn (PCM in → STT → respond → TTS → PCM out).

The full voice pipeline lives in `skchat.transports.websocket._process_speech`:
it flushes a PCM buffer, runs STT, calls `VoiceEngine.respond`, synthesizes the
reply via TTS, and pushes the resulting audio bytes back over the WebSocket.

This test mocks all three stages (STT / respond / TTS) plus the WebSocket so no
models, network, or live endpoints are needed. It asserts:
  * each stage (STT, respond, TTS) is invoked exactly once,
  * the stages are wired in the right order (transcript → response → audio),
  * the synthesized PCM bytes are sent back out over the socket.

All state is local to each test — no shared fixtures.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from skchat.transports.websocket import _process_speech


class _FakeWS:
    """Captures send_json / send_bytes calls (the transport's only WS use here)."""

    def __init__(self) -> None:
        self.json_msgs: list[dict] = []
        self.byte_msgs: list[bytes] = []

    async def send_json(self, msg: dict) -> None:
        self.json_msgs.append(msg)

    async def send_bytes(self, data: bytes) -> None:
        self.byte_msgs.append(data)


def _make_engine(transcript: str, response: str, audio: bytes):
    """Build a fully-mocked engine: stt.transcribe, respond, tts.synthesize."""
    engine = AsyncMock()
    # cfg.tts_voice is read by _synthesize; give it a concrete value.
    engine.cfg.tts_voice = "lumina"

    engine.stt = AsyncMock()
    engine.stt.transcribe = AsyncMock(return_value=transcript)
    engine.respond = AsyncMock(return_value=response)
    engine.tts = AsyncMock()
    engine.tts.synthesize = AsyncMock(return_value=audio)
    return engine


def _run_turn(engine, pcm_in: bytes) -> _FakeWS:
    ws = _FakeWS()
    asyncio.run(
        _process_speech(
            ws,
            pcm_in,
            history=[],
            engine=engine,
            agent_name="lumina",
            group_suffix=[],
            conn_id="conn-1",
            pending_group={},
        )
    )
    return ws


def test_voice_turn_returns_pcm_and_calls_each_stage_once():
    pcm_in = b"\x00\x01" * 800  # fake 16-bit PCM input
    pcm_out = b"\x10\x20" * 1200  # fake synthesized audio out
    engine = _make_engine("hello there", "hi back", pcm_out)

    ws = _run_turn(engine, pcm_in)

    # Each stage of the pipeline runs exactly once.
    engine.stt.transcribe.assert_awaited_once()
    engine.respond.assert_awaited_once()
    engine.tts.synthesize.assert_awaited_once()

    # PCM (audio) bytes made it back out over the socket.
    assert ws.byte_msgs == [pcm_out]


def test_voice_turn_wires_transcript_into_respond_and_tts():
    pcm_out = b"\xab\xcd" * 64
    engine = _make_engine("what time is it", "it is noon", pcm_out)

    _run_turn(engine, b"\x00\x00" * 400)

    # STT transcript is passed to respond as the user input.
    respond_args = engine.respond.await_args
    assert "what time is it" in respond_args.args[0]

    # respond's reply text is what gets synthesized to audio.
    synth_args = engine.tts.synthesize.await_args
    assert synth_args.args[0] == "it is noon"
    # Voice comes from cfg.tts_voice.
    assert synth_args.kwargs.get("voice") == "lumina"


def test_voice_turn_emits_user_and_assistant_transcripts():
    engine = _make_engine("good morning", "morning, chef", b"\x01\x02" * 32)

    ws = _run_turn(engine, b"\x00\x00" * 200)

    transcripts = [m for m in ws.json_msgs if m.get("type") == "transcript"]
    roles = {m["role"]: m["text"] for m in transcripts}
    assert roles.get("user") == "good morning"
    assert roles.get("assistant") == "morning, chef"


def test_empty_transcript_skips_respond_and_tts():
    """Silence / empty STT result must short-circuit before respond + TTS."""
    engine = _make_engine("", "should not be used", b"unused")

    ws = _run_turn(engine, b"\x00\x00" * 100)

    engine.stt.transcribe.assert_awaited_once()
    engine.respond.assert_not_awaited()
    engine.tts.synthesize.assert_not_awaited()
    assert ws.byte_msgs == []
