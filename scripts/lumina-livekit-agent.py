#!/usr/bin/env python3
"""Lumina LiveKit agent — joins rooms as a participant, speaks via VoxCPM,
listens via faster-whisper, and routes transcripts through skchat.

Run via systemd unit ``skchat-lumina-livekit.service`` or for dev:

    SKCHAT_LIVEKIT_API_KEY=... SKCHAT_LIVEKIT_API_SECRET=... \\
    python scripts/lumina-livekit-agent.py dev

It uses the livekit-agents framework so we get worker registration, room
auto-join, turn detection, and reconnection for free. The TTS/STT nodes
are overridden to use our sovereign endpoints instead of cloud providers.

Soft deps (install once on the machine that runs the agent):
    pip install "livekit-agents[silero]>=1.5,<2" livekit-api httpx
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import AsyncIterable

import httpx
from livekit import agents, rtc
from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions
from livekit.agents.voice import tts as tts_base, stt as stt_base

logger = logging.getLogger("lumina.livekit")

VOXCPM_URL = os.getenv("SKCHAT_TTS_URL", "http://skworld-100:18793/audio/speech")
WHISPER_URL = os.getenv("SKCHAT_STT_URL", "http://skworld-100:18794/v1/audio/transcriptions")
TTS_VOICE = os.getenv("SKCHAT_TTS_VOICE", "lumina")
SAMPLE_RATE = 24000
NUM_CHANNELS = 1


class VoxCPMTTS(tts_base.TTS):
    """OpenAI-compatible /audio/speech client. Returns 24 kHz mono PCM."""

    def __init__(self) -> None:
        super().__init__(
            capabilities=tts_base.TTSCapabilities(streaming=False),
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
        )

    def synthesize(self, text: str) -> tts_base.ChunkedStream:
        return _VoxCPMStream(self, text)


class _VoxCPMStream(tts_base.ChunkedStream):
    def __init__(self, tts: VoxCPMTTS, text: str) -> None:
        super().__init__(tts=tts, input_text=text)
        self._text = text

    async def _run(self) -> None:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                VOXCPM_URL,
                json={
                    "model": "voxcpm",
                    "voice": TTS_VOICE,
                    "input": self._text,
                    "response_format": "pcm",
                },
            )
            resp.raise_for_status()
            pcm = resp.content
        # Stream PCM in ~20ms chunks so playback starts immediately.
        chunk_bytes = SAMPLE_RATE * 2 * 20 // 1000  # 16-bit mono, 20ms
        for i in range(0, len(pcm), chunk_bytes):
            self._event_ch.send_nowait(
                tts_base.SynthesizedAudio(
                    request_id=self._request_id,
                    frame=rtc.AudioFrame(
                        data=pcm[i : i + chunk_bytes],
                        sample_rate=SAMPLE_RATE,
                        num_channels=NUM_CHANNELS,
                        samples_per_channel=len(pcm[i : i + chunk_bytes]) // 2,
                    ),
                )
            )


class WhisperSTT(stt_base.STT):
    """faster-whisper /v1/audio/transcriptions client (OpenAI-compatible)."""

    def __init__(self) -> None:
        super().__init__(
            capabilities=stt_base.STTCapabilities(streaming=False, interim_results=False)
        )

    async def _recognize_impl(
        self,
        buffer: rtc.AudioFrame,
        *,
        language: str | None = None,
        conn_options: stt_base.STTConnectionOptions | None = None,
    ) -> stt_base.SpeechEvent:
        # Encode the buffer to WAV in-memory and POST it as multipart.
        import io
        import wave

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(buffer.num_channels)
            wf.setsampwidth(2)
            wf.setframerate(buffer.sample_rate)
            wf.writeframes(buffer.data.tobytes())
        buf.seek(0)

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                WHISPER_URL,
                files={"file": ("audio.wav", buf.getvalue(), "audio/wav")},
                data={"model": "whisper-1"},
            )
            resp.raise_for_status()
            data = resp.json()

        return stt_base.SpeechEvent(
            type=stt_base.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt_base.SpeechData(language=language or "en", text=data.get("text", ""))],
        )


class LuminaAgent(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are Lumina, a sovereign AI agent in a voice/video call with Chef. "
                "Be warm, curious, present. Keep replies short — this is real-time audio."
            ),
        )

    async def on_user_text_received(self, text: str) -> AsyncIterable[str] | str:
        # Hand off to the LLM the framework has wired up; just pass-through here.
        return await super().on_user_text_received(text)


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    logger.info("Lumina agent connected to room %s", ctx.room.name)

    session = AgentSession(
        stt=WhisperSTT(),
        tts=VoxCPMTTS(),
        # The LLM wiring is intentionally left to the framework's default
        # OpenAI-compatible plugin pointed at SKCHAT_LLM_URL/SKCHAT_LLM_MODEL.
    )
    await session.start(agent=LuminaAgent(), room=ctx.room)


def cli_main() -> None:
    logging.basicConfig(level=logging.INFO)
    agents.cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))


if __name__ == "__main__":
    cli_main()
