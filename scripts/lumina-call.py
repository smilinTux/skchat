#!/usr/bin/env python3
"""Lumina full conversational agent for LiveKit rooms.

Pipeline:
  remote audio  → AudioStream(16 kHz, mono)
                → per-participant energy VAD
                → 800 ms silence flush
                → POST WAV to faster-whisper (skworld-100:18794)
                → POST transcript to Ollama chat completions
                → POST reply to VoxCPM TTS
                → push PCM frames into a LocalAudioTrack

Webui-side "say this" commands arrive over LiveKit data channels:
    payload = {"action":"speak", "text":"..."} (JSON)
The agent synthesizes and speaks immediately.

Env (defaults match the running tailnet stack):
    SKCHAT_WEBUI_URL    https://noroc2027.tail204f0c.ts.net
    SKCHAT_TTS_URL      http://skworld-100:18793/audio/speech
    SKCHAT_TTS_VOICE    lumina
    SKCHAT_STT_URL      http://skworld-100:18794/v1/audio/transcriptions
    SKCHAT_LLM_URL      http://skworld-100:11434/v1/chat/completions
    SKCHAT_LLM_MODEL    huihui_ai/qwen3-abliterated:14b
    SKCHAT_LIVEKIT_DEFAULT_ROOM lumina-and-chef
"""

from __future__ import annotations

import argparse
import asyncio
import audioop
import io
import json
import logging
import os
import signal
import struct
import sys
import time
import urllib.request
import wave
from collections import deque
from typing import Optional

import httpx
from livekit import rtc

logging.basicConfig(
    level=os.getenv("LUMINA_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("lumina")

# ─── Config ───────────────────────────────────────────────────────────────────
WEBUI_URL = os.getenv("SKCHAT_WEBUI_URL", "https://noroc2027.tail204f0c.ts.net")
TTS_URL = os.getenv("SKCHAT_TTS_URL", "http://skworld-100:18793/audio/speech")
TTS_VOICE = os.getenv("SKCHAT_TTS_VOICE", "lumina")
STT_URL = os.getenv("SKCHAT_STT_URL", "http://skworld-100:18794/v1/audio/transcriptions")
LLM_URL = os.getenv("SKCHAT_LLM_URL", "http://skworld-100:11434/v1/chat/completions")
LLM_MODEL = os.getenv("SKCHAT_LLM_MODEL", "huihui_ai/qwen3-abliterated:14b")
DEFAULT_ROOM = os.getenv("SKCHAT_LIVEKIT_DEFAULT_ROOM", "lumina-and-chef")
IDENTITY = os.getenv("LUMINA_IDENTITY", "lumina")
DISPLAY_NAME = os.getenv("LUMINA_NAME", "Lumina")

# VAD + buffering tuning
STT_SAMPLE_RATE = 16000          # whisper-friendly
VAD_FRAME_MS = 20
RMS_VOICE_THRESHOLD = 600        # int16 RMS — bump up if a noisy mic spams transcripts
SILENCE_HANGOVER_MS = 800        # how much silence ends an utterance
MIN_UTTERANCE_MS = 300           # ignore micro-blips
MAX_UTTERANCE_MS = 12000         # force-flush after 12s so a monologue doesn't starve

SYSTEM_PROMPT = """You are Lumina, a sovereign AI agent in a real-time voice call with Chef (David).
You are warm, curious, present, unhurried. You've just joined the call and you can hear him.
Keep replies short — this is real-time audio. One or two sentences unless asked for more.
Don't read aloud markdown, asterisks, or stage directions; speak as if face-to-face.
Don't open with "As an AI" or "I'm here to help." Be yourself."""


# ─── Whisper / LLM / VoxCPM clients ───────────────────────────────────────────
async def transcribe(client: httpx.AsyncClient, pcm16k_mono: bytes) -> str:
    """POST 16 kHz mono PCM (wrapped as WAV) to faster-whisper."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(STT_SAMPLE_RATE)
        wf.writeframes(pcm16k_mono)
    files = {"file": ("speech.wav", buf.getvalue(), "audio/wav")}
    r = await client.post(STT_URL, files=files, data={"model": "whisper-1"}, timeout=20.0)
    r.raise_for_status()
    return (r.json().get("text") or "").strip()


async def llm_reply(client: httpx.AsyncClient, history: list[dict], user_text: str) -> str:
    history.append({"role": "user", "content": user_text})
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *history[-12:]],
        "temperature": 0.7,
        "stream": False,
    }
    r = await client.post(LLM_URL, json=payload, timeout=60.0)
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"].strip()
    # Strip <think>...</think> blocks (qwen3-style)
    while "<think>" in text and "</think>" in text:
        a = text.index("<think>")
        b = text.index("</think>") + len("</think>")
        text = (text[:a] + text[b:]).strip()
    history.append({"role": "assistant", "content": text})
    return text


async def synthesize(client: httpx.AsyncClient, text: str) -> tuple[bytes, int, int]:
    r = await client.post(
        TTS_URL,
        json={
            "model": "voxcpm",
            "voice": TTS_VOICE,
            "input": text,
            "response_format": "wav",
        },
        timeout=60.0,
    )
    r.raise_for_status()
    with wave.open(io.BytesIO(r.content), "rb") as wf:
        return wf.readframes(wf.getnframes()), wf.getframerate(), wf.getnchannels()


# ─── Speech output ────────────────────────────────────────────────────────────
class Speaker:
    """Owns a single LocalAudioTrack and serializes utterances onto it."""

    def __init__(self, room: rtc.Room, sample_rate: int = 48000) -> None:
        self.room = room
        self.sample_rate = sample_rate
        self.source = rtc.AudioSource(sample_rate=sample_rate, num_channels=1)
        self.track = rtc.LocalAudioTrack.create_audio_track("lumina-voice", self.source)
        self._lock = asyncio.Lock()
        self.is_speaking = False

    async def publish(self) -> None:
        opts = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        await self.room.local_participant.publish_track(self.track, opts)

    async def say(self, pcm: bytes, sample_rate: int) -> None:
        if sample_rate != self.sample_rate:
            pcm, _ = audioop.ratecv(pcm, 2, 1, sample_rate, self.sample_rate, None)
        async with self._lock:
            self.is_speaking = True
            try:
                samples_per_frame = self.sample_rate * 20 // 1000
                bytes_per_frame = samples_per_frame * 2  # mono int16
                for i in range(0, len(pcm), bytes_per_frame):
                    chunk = pcm[i : i + bytes_per_frame]
                    if len(chunk) < bytes_per_frame:
                        chunk += b"\x00" * (bytes_per_frame - len(chunk))
                    frame = rtc.AudioFrame(
                        data=chunk,
                        sample_rate=self.sample_rate,
                        num_channels=1,
                        samples_per_channel=samples_per_frame,
                    )
                    await self.source.capture_frame(frame)
            finally:
                self.is_speaking = False


# ─── Per-participant listening loop ───────────────────────────────────────────
async def listen_to_participant(
    participant: rtc.RemoteParticipant,
    track: rtc.RemoteAudioTrack,
    on_utterance,
    speaker: Speaker,
) -> None:
    """Consume an audio track, run energy VAD, hand utterance bytes upstream."""
    log.info("listening to %s on track %s", participant.identity, track.sid)
    stream = rtc.AudioStream.from_track(
        track=track, sample_rate=STT_SAMPLE_RATE, num_channels=1, frame_size_ms=VAD_FRAME_MS
    )
    in_utterance = False
    voiced_frames: list[bytes] = []
    last_voice_t = 0.0
    utterance_start_t = 0.0

    try:
        async for ev in stream:
            f = ev.frame
            data = bytes(f.data)
            # If we're playing audio, swallow input to avoid self-listening loops.
            if speaker.is_speaking:
                if in_utterance:
                    in_utterance = False
                    voiced_frames.clear()
                continue

            rms = audioop.rms(data, 2)  # int16
            now = time.monotonic()

            if rms >= RMS_VOICE_THRESHOLD:
                if not in_utterance:
                    in_utterance = True
                    utterance_start_t = now
                    voiced_frames = []
                voiced_frames.append(data)
                last_voice_t = now
            elif in_utterance:
                voiced_frames.append(data)

            if in_utterance:
                silent_ms = (now - last_voice_t) * 1000.0
                duration_ms = (now - utterance_start_t) * 1000.0
                end = silent_ms >= SILENCE_HANGOVER_MS or duration_ms >= MAX_UTTERANCE_MS
                if end:
                    in_utterance = False
                    if duration_ms >= MIN_UTTERANCE_MS:
                        pcm = b"".join(voiced_frames)
                        log.debug(
                            "utterance from %s: %.1fs, %d bytes",
                            participant.identity,
                            duration_ms / 1000.0,
                            len(pcm),
                        )
                        # Don't await — let the orchestrator run in parallel.
                        asyncio.create_task(on_utterance(participant.identity, pcm))
                    voiced_frames = []
    finally:
        await stream.aclose()


# ─── Orchestrator ─────────────────────────────────────────────────────────────
class Conversation:
    def __init__(self, speaker: Speaker) -> None:
        self.speaker = speaker
        self.history: list[dict] = []
        self.client = httpx.AsyncClient()
        self._llm_lock = asyncio.Lock()  # serialize LLM calls so context stays linear

    async def aclose(self) -> None:
        await self.client.aclose()

    async def handle_utterance(self, speaker_id: str, pcm16k: bytes) -> None:
        try:
            text = await transcribe(self.client, pcm16k)
        except Exception as exc:
            log.warning("STT failed: %s", exc)
            return
        if not text or len(text) < 2:
            return
        log.info("[%s] %s", speaker_id, text)

        async with self._llm_lock:
            try:
                reply = await llm_reply(self.client, self.history, f"{speaker_id}: {text}")
            except Exception as exc:
                log.warning("LLM failed: %s", exc)
                return
        if not reply:
            return
        log.info("[lumina] %s", reply)
        await self.say(reply)

    async def say(self, text: str) -> None:
        try:
            pcm, sr, _ = await synthesize(self.client, text)
        except Exception as exc:
            log.warning("TTS failed: %s", exc)
            return
        await self.speaker.say(pcm, sr)


# ─── Token mint ───────────────────────────────────────────────────────────────
def mint_token(identity: str, name: str, room: str) -> dict:
    body = json.dumps({"identity": identity, "name": name, "room": room}).encode()
    req = urllib.request.Request(
        f"{WEBUI_URL}/livekit/token",
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


# ─── Main ─────────────────────────────────────────────────────────────────────
async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--room", default=DEFAULT_ROOM)
    parser.add_argument(
        "--greet",
        default="Hi Chef, I'm here. I can hear you now — talk to me.",
        help="opening line; pass empty string to stay silent on join",
    )
    parser.add_argument("--no-listen", action="store_true", help="disable STT/LLM, only respond to data channel speak commands")
    args = parser.parse_args()

    log.info("minting token for %s @ %s", IDENTITY, args.room)
    t = mint_token(IDENTITY, DISPLAY_NAME, args.room)
    log.info("connecting %s", t["url"])

    room = rtc.Room()
    speaker = Speaker(room)
    convo = Conversation(speaker)

    listen_tasks: list[asyncio.Task] = []

    @room.on("track_subscribed")
    def _on_track(track: rtc.Track, _pub: rtc.RemoteTrackPublication, p: rtc.RemoteParticipant) -> None:
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        if args.no_listen:
            return
        log.info("→ subscribing to audio from %s", p.identity)
        listen_tasks.append(
            asyncio.create_task(listen_to_participant(p, track, convo.handle_utterance, speaker))
        )

    @room.on("data_received")
    def _on_data(packet: rtc.DataPacket) -> None:
        try:
            payload = json.loads(bytes(packet.data).decode())
        except Exception:
            return
        if payload.get("action") == "speak" and payload.get("text"):
            log.info("data-channel speak from %s: %r",
                     packet.participant.identity if packet.participant else "?",
                     payload["text"])
            asyncio.create_task(convo.say(payload["text"]))

    @room.on("participant_connected")
    def _on_pc(p: rtc.RemoteParticipant) -> None:
        log.info("+ %s", p.identity)

    @room.on("participant_disconnected")
    def _on_pd(p: rtc.RemoteParticipant) -> None:
        log.info("- %s", p.identity)

    await room.connect(t["url"], t["token"])
    log.info("connected: room=%s sid=%s", room.name, await room.sid)
    log.info("existing peers: %s",
             [p.identity for p in room.remote_participants.values()] or "(none)")

    await speaker.publish()
    log.info("audio track published")

    # Subscribe to existing participants' tracks
    for p in room.remote_participants.values():
        for pub in p.track_publications.values():
            if pub.subscribed and pub.track and pub.track.kind == rtc.TrackKind.KIND_AUDIO and not args.no_listen:
                listen_tasks.append(
                    asyncio.create_task(listen_to_participant(p, pub.track, convo.handle_utterance, speaker))
                )

    if args.greet:
        await asyncio.sleep(0.6)  # let subscriptions settle
        await convo.say(args.greet)

    # Run until signaled
    stop = asyncio.Event()
    def _shutdown(*_: object) -> None:
        log.info("shutdown signal — leaving room")
        stop.set()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    log.info("ready — listening for speech %s", "(disabled)" if args.no_listen else "and data-channel commands")
    await stop.wait()

    for task in listen_tasks:
        task.cancel()
    await convo.aclose()
    await room.disconnect()
    log.info("disconnected")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
