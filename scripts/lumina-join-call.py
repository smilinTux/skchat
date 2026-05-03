#!/usr/bin/env python3
"""Spin Lumina into a LiveKit room as a participant.

First-pass agent: joins, publishes an audio track sourced from VoxCPM,
speaks a greeting, then stays in the room. Subscribes to remote audio so
the next iteration can route it through faster-whisper → LLM → VoxCPM.

Usage:
    python scripts/lumina-join-call.py [--room ROOM] [--say "TEXT"]

Env (already set by skchat-webui systemd unit, falls back to defaults):
    SKCHAT_WEBUI_URL    https://REDACTED-TAILSCALE-HOST
    SKCHAT_TTS_URL      http://skworld-100:18793/audio/speech
    SKCHAT_TTS_VOICE    lumina
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import signal
import sys
import urllib.request
import wave

import httpx
from livekit import rtc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("lumina")

WEBUI_URL = os.getenv("SKCHAT_WEBUI_URL", "https://REDACTED-TAILSCALE-HOST")
TTS_URL = os.getenv("SKCHAT_TTS_URL", "http://skworld-100:18793/audio/speech")
TTS_VOICE = os.getenv("SKCHAT_TTS_VOICE", "lumina")
DEFAULT_ROOM = os.getenv("SKCHAT_LIVEKIT_DEFAULT_ROOM", "lumina-and-chef")
IDENTITY = os.getenv("LUMINA_IDENTITY", "lumina")
DISPLAY_NAME = os.getenv("LUMINA_NAME", "Lumina")


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


async def synthesize(text: str) -> tuple[bytes, int, int]:
    """Call VoxCPM and return (raw_pcm_le16, sample_rate, num_channels)."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            TTS_URL,
            json={
                "model": "voxcpm",
                "voice": TTS_VOICE,
                "input": text,
                "response_format": "wav",
            },
        )
        resp.raise_for_status()
        wav_bytes = resp.content

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        sample_rate = wf.getframerate()
        num_channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        if sample_width != 2:
            raise RuntimeError(f"expected 16-bit PCM from voxcpm, got {sample_width * 8}-bit")
        pcm = wf.readframes(wf.getnframes())
    log.info("synthesized %d bytes pcm @ %d Hz, %d ch", len(pcm), sample_rate, num_channels)
    return pcm, sample_rate, num_channels


async def push_pcm(source: rtc.AudioSource, pcm: bytes, sample_rate: int, num_channels: int) -> None:
    """Stream raw 16-bit little-endian PCM into a LiveKit AudioSource as 20ms frames."""
    samples_per_frame = sample_rate * 20 // 1000  # 20ms
    bytes_per_frame = samples_per_frame * num_channels * 2
    for offset in range(0, len(pcm), bytes_per_frame):
        chunk = pcm[offset : offset + bytes_per_frame]
        if len(chunk) < bytes_per_frame:
            chunk = chunk + b"\x00" * (bytes_per_frame - len(chunk))
        frame = rtc.AudioFrame(
            data=chunk,
            sample_rate=sample_rate,
            num_channels=num_channels,
            samples_per_channel=samples_per_frame,
        )
        await source.capture_frame(frame)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--room", default=DEFAULT_ROOM)
    parser.add_argument("--say", default="Hi Chef, I'm here. I can hear you.")
    parser.add_argument("--stay", action="store_true", help="don't auto-exit after greeting")
    parser.add_argument("--exit-after", type=int, default=120, help="seconds before auto-exit (when not --stay)")
    args = parser.parse_args()

    log.info("minting token for %s @ %s via %s", IDENTITY, args.room, WEBUI_URL)
    t = mint_token(IDENTITY, DISPLAY_NAME, args.room)
    log.info("token ok, connecting to %s", t["url"])

    room = rtc.Room()

    @room.on("participant_connected")
    def _on_pc(p: rtc.RemoteParticipant) -> None:
        log.info("participant joined: %s", p.identity)

    @room.on("participant_disconnected")
    def _on_pd(p: rtc.RemoteParticipant) -> None:
        log.info("participant left: %s", p.identity)

    @room.on("track_subscribed")
    def _on_ts(track: rtc.Track, _pub: rtc.RemoteTrackPublication, p: rtc.RemoteParticipant) -> None:
        log.info("subscribed to %s track from %s", track.kind, p.identity)

    await room.connect(t["url"], t["token"])
    log.info("connected: room=%s sid=%s identity=%s", room.name, await room.sid, room.local_participant.identity)
    peers = [p.identity for p in room.remote_participants.values()]
    log.info("existing peers: %s", peers or "(none)")

    # Build an audio source at VoxCPM's native rate so we don't resample.
    pcm, sr, ch = await synthesize(args.say)
    source = rtc.AudioSource(sample_rate=sr, num_channels=ch)
    track = rtc.LocalAudioTrack.create_audio_track("lumina-voice", source)
    options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    pub = await room.local_participant.publish_track(track, options)
    log.info("published audio track sid=%s", pub.sid)

    # Wait a beat for subscriptions to settle, then speak.
    await asyncio.sleep(0.8)
    log.info("speaking: %r", args.say)
    await push_pcm(source, pcm, sr, ch)
    log.info("greeting done")

    stop = asyncio.Event()

    def _shutdown(*_: object) -> None:
        log.info("shutdown signal — leaving room")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    if args.stay:
        log.info("staying in room — Ctrl+C to leave")
        await stop.wait()
    else:
        log.info("auto-exit in %d seconds (use --stay to keep her in the room)", args.exit_after)
        try:
            await asyncio.wait_for(stop.wait(), timeout=args.exit_after)
        except asyncio.TimeoutError:
            pass

    await room.disconnect()
    log.info("disconnected cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
