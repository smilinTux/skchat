"""Subscribe to Lumina's audio track and save to a WAV file.

Spawned by the webui's /livekit/record/start endpoint. Subscribes via the
LiveKit Python SDK, captures Lumina's published audio frames at 48kHz mono,
writes them out as a single WAV file to ~/.skchat/lumina-recordings/.

Killed via SIGTERM by the webui's /livekit/record/stop endpoint — the SIGTERM
handler closes the WAV cleanly so partial captures aren't lost.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import urllib.request
import wave
from pathlib import Path

from livekit import rtc

DEFAULT_TARGET = "lumina"
DEFAULT_ROOM = "lumina-and-chef"
WEBUI_URL = "https://noroc2027.tail204f0c.ts.net"
SAMPLE_RATE = 48000


def mint_token(identity: str, room: str, webui_url: str) -> dict:
    body = json.dumps({"identity": identity, "name": identity, "room": room}).encode()
    req = urllib.request.Request(
        f"{webui_url}/livekit/token",
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="Output WAV file path")
    parser.add_argument("--room", default=DEFAULT_ROOM)
    parser.add_argument("--target", default=DEFAULT_TARGET, help="Identity to record (default: lumina)")
    parser.add_argument("--webui", default=WEBUI_URL)
    parser.add_argument("--identity", default="recorder")
    args = parser.parse_args()

    out_path = Path(args.out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wav = wave.open(str(out_path), "wb")
    wav.setnchannels(1)
    wav.setsampwidth(2)
    wav.setframerate(SAMPLE_RATE)

    bytes_written = 0
    stop = asyncio.Event()

    def _handle_signal(*_: object) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, _handle_signal)

    t = mint_token(args.identity, args.room, args.webui)
    room = rtc.Room()
    consumer_task: asyncio.Task | None = None

    @room.on("track_subscribed")
    def _on_track(track: rtc.Track, _pub, p: rtc.RemoteParticipant) -> None:
        nonlocal consumer_task
        if p.identity != args.target or track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        if consumer_task is not None:
            return  # already consuming

        async def consume() -> None:
            nonlocal bytes_written
            stream = rtc.AudioStream.from_track(track=track, sample_rate=SAMPLE_RATE, num_channels=1)
            async for ev in stream:
                data = bytes(ev.frame.data)
                wav.writeframes(data)
                bytes_written += len(data)

        consumer_task = asyncio.create_task(consume())

    await room.connect(t["url"], t["token"])
    print(f"recording {args.target} → {out_path}")
    await stop.wait()
    print(f"\nstopping — wrote {bytes_written} bytes ({bytes_written/(SAMPLE_RATE*2):.1f}s)")
    if consumer_task is not None:
        consumer_task.cancel()
    await room.disconnect()
    wav.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
