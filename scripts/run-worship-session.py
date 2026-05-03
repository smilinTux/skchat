#!/usr/bin/env python3
"""Standalone driver for a 15-scene worship session.

Headless: no FaceTime / lumina-call wrapper required. Builds the session
through skchat.worship, writes a status file each phase, dumps the contact
sheet + manifest path on completion.

Status file: ~/.skcapstone/agents/lumina/memory/worship-sessions/<session_id>/status.txt
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

from skchat.worship import WORSHIP_HOME, WorshipSession


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", help="user_prompt — the scene scaffold")
    ap.add_argument("--session-id",
                    default=f"ws_{int(time.time())}_{Path('.').resolve().name[:6]}")
    ap.add_argument("--scenes", type=int, default=15)
    args = ap.parse_args()

    sess = WorshipSession(
        session_id=args.session_id,
        user_prompt=args.prompt,
        image_count=args.scenes,
    )
    status_file = sess.home / "status.txt"

    async def on_status(msg: str) -> None:
        status_file.write_text(f"{time.time():.0f}\t{msg}\n", encoding="utf-8")
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    sess.on_status = on_status

    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=30.0)) as client:
        await sess.generate(client)

    rendered = sum(1 for s in sess.scenes if s.image_path)
    final = {
        "session_id": sess.session_id,
        "home": str(sess.home),
        "rendered": rendered,
        "total": sess.image_count,
        "audio": str(sess.audio_path) if sess.audio_path else None,
        "audio_s": round(sess.audio_duration_s, 1),
        "manifest": str(sess.home / "manifest.json"),
        "status": sess.status,
    }
    print(json.dumps(final, indent=2), flush=True)
    (sess.home / "final.json").write_text(json.dumps(final, indent=2),
                                          encoding="utf-8")
    return 0 if rendered > 0 and sess.audio_path else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
