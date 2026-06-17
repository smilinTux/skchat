#!/usr/bin/env python3
"""Minimal in-memory Nostr relay — enough for SK federation discovery (U8).

Speaks the subset the federation path uses: EVENT (store + OK), REQ (return
stored events matching the filter's `kinds`, then EOSE), CLOSE (noop). Holds
events in memory (lost on restart). Not a general-purpose relay — a sovereign
tailnet discovery relay for SFU focus descriptors (kind 30078) etc.

Env: NOSTR_RELAY_HOST (default 127.0.0.1), NOSTR_RELAY_PORT (default 7447)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("nostr-relay")

HOST = os.environ.get("NOSTR_RELAY_HOST", "127.0.0.1")
PORT = int(os.environ.get("NOSTR_RELAY_PORT", "7447"))
_events: list[dict] = []


def _matches(ev: dict, filt: dict) -> bool:
    kinds = filt.get("kinds")
    if kinds and ev.get("kind") not in kinds:
        return False
    authors = filt.get("authors")
    if authors and ev.get("pubkey") not in authors:
        return False
    return True


async def handler(ws):
    async for raw in ws:
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        if not isinstance(msg, list) or not msg:
            continue
        verb = msg[0]
        if verb == "EVENT" and len(msg) >= 2:
            ev = msg[1]
            _events.append(ev)
            log.info("stored event kind=%s id=%s", ev.get("kind"), str(ev.get("id"))[:12])
            await ws.send(json.dumps(["OK", ev.get("id", ""), True, ""]))
        elif verb == "REQ" and len(msg) >= 2:
            subid = msg[1]
            filt = msg[2] if len(msg) >= 3 else {}
            for ev in _events:
                if _matches(ev, filt):
                    await ws.send(json.dumps(["EVENT", subid, ev]))
            await ws.send(json.dumps(["EOSE", subid]))
        elif verb == "CLOSE":
            pass


async def main():
    log.info("nostr relay listening on ws://%s:%d", HOST, PORT)
    async with websockets.serve(handler, HOST, PORT):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
