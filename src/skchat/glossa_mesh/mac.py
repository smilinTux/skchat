"""Carrier-sense MAC + FakeAudioMedium (spec §7). The mixed audio medium collides
if two transmit at once; the MAC is listen-before-talk (acquire the medium, then
transmit). FakeAudioMedium simulates the shared channel + collision detection."""

from __future__ import annotations

import asyncio


class FakeAudioMedium:
    """In-memory shared audio channel. transmit_raw is UNGUARDED (used to prove
    collisions); send via a MAC to serialize."""

    def __init__(self) -> None:
        self._busy = False
        self.had_collision = False
        self.transmissions = 0
        self._listeners: list = []

    def is_busy(self) -> bool:
        return self._busy

    def on_receive(self, cb) -> None:
        self._listeners.append(cb)

    async def transmit_raw(self, src: str, samples: list) -> None:
        if self._busy:
            self.had_collision = True  # someone else is already transmitting
        self._busy = True
        try:
            await asyncio.sleep(0)  # yield — lets a concurrent tx overlap
            self.transmissions += 1
            for cb in self._listeners:
                cb(src, samples)
        finally:
            self._busy = False


class CarrierSenseMAC:
    """Listen-before-talk: serialize transmits on the shared medium with a lock so
    no two overlap (real radios sense the carrier; here a lock models the channel)."""

    def __init__(self, medium: FakeAudioMedium) -> None:
        self._medium = medium
        self._lock = asyncio.Lock()

    async def send(self, src: str, samples: list) -> None:
        async with self._lock:  # acquire the channel
            await self._medium.transmit_raw(src, samples)
