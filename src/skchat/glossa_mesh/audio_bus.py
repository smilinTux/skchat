"""AudioMeshBus (spec §7) — MeshBus over the acoustic medium: modulate frames to
FSK tones, transmit via the carrier-sense MAC, demodulate on receive. A drop-in for
G3's data-channel bus, so a GlossaMeshNode meshes over AUDIO unchanged."""

from __future__ import annotations

from skchat.glossa_mesh.bus import MeshBus, ReceiveCb
from skchat.glossa_mesh.mac import CarrierSenseMAC, FakeAudioMedium
from skchat.glossa_mesh.modem import AudioModem


class AudioMeshBus(MeshBus):
    def __init__(self, member_id: str, medium: FakeAudioMedium) -> None:
        self.member_id = member_id
        self._medium = medium
        self._mac = CarrierSenseMAC(medium)
        self._modem = AudioModem()
        self._cb: ReceiveCb | None = None
        self.running = False
        medium.on_receive(self._on_samples)

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def broadcast(self, data: bytes) -> None:
        if not self.running:
            raise RuntimeError("bus not started")
        await self._mac.send(self.member_id, self._modem.encode(data))

    def on_receive(self, cb: ReceiveCb) -> None:
        self._cb = cb

    def on_leave(self, cb) -> None:   # G3 seam; audio medium has no presence yet
        pass

    def _on_samples(self, src: str, samples: list) -> None:
        if src == self.member_id or not self.running:
            return                      # don't demodulate our own transmission
        if self._cb is not None:
            self._cb(self._modem.decode(samples), src)
