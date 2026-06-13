# SKGlossa G5a — Audio soft-modem + MAC (acoustic mesh tier)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Repo: `skchat`, branch `feat/skglossa-g5a-audio` (cut from `feat/sk-spaces`, so the G3 `glossa_mesh` package is present). Run tests from repo root: `~/.skenv/bin/python -m pytest tests/ -q`.

**Goal:** The wild tier — SKGlossa as **audio tones** over a Space's audio track, a true *shared-medium acoustic network*. A pure-Python FSK soft-modem (bytes ↔ audio samples) + a **carrier-sense MAC** (so ≥2 agents don't collide on the mixed audio medium) + an `AudioMeshBus` that plugs into the G3 `MeshBus` seam. All CI-tested with an in-memory audio medium — no real audio hardware.

**Architecture:** `AudioModem` does FSK encode/decode (mark/space tones; pure-Python energy correlation, no numpy dependency). `CarrierSenseMAC` gates transmits on whether the shared medium is busy (listen-before-talk). `AudioMeshBus(MeshBus)` ties modem+MAC over a `FakeAudioMedium` (CI) — and is a drop-in alternative to G3's data-channel bus, so a `GlossaMeshNode` meshes over *audio* unchanged. The real LiveKit-audio-track binding is a later phase.

**Tech Stack:** Python 3.10+ (stdlib `math` only — no numpy), reuses `skchat.glossa_mesh` (G3: `MeshBus`, `GlossaMeshNode`) + `skcomms.glossa`. `pytest` + pytest-asyncio (`asyncio_mode=auto`). Line 99, ruff.

**Spec:** `skcomms` `docs/superpowers/specs/2026-06-13-skglossa-design.md` §7 (audio soft-modem mesh; MAC first-class). **Depends on:** G3 (mesh package, on `feat/sk-spaces`).

---

## Task 1: `AudioModem` — FSK bytes ↔ samples

**Files:** Create `src/skchat/glossa_mesh/modem.py`. Test `tests/test_glossa_modem.py`.

FSK: each bit → `SAMPLES_PER_BIT` samples of a sine at `MARK_HZ` (1) or `SPACE_HZ`
(0). Decode per bit-window by comparing correlation energy at the two frequencies.
Noiseless CI samples decode exactly.

- [ ] **Step 1: Failing test** — `tests/test_glossa_modem.py`:

```python
from skchat.glossa_mesh.modem import AudioModem


def test_byte_roundtrip_through_samples():
    m = AudioModem()
    data = b"hello-glossa"
    samples = m.encode(data)
    assert isinstance(samples, list) and len(samples) > 0
    assert m.decode(samples) == data


def test_empty_and_binary_roundtrip():
    m = AudioModem()
    assert m.decode(m.encode(b"")) == b""
    payload = bytes(range(256))
    assert m.decode(m.encode(payload)) == payload


def test_decode_tolerates_mild_amplitude_scaling():
    m = AudioModem()
    samples = [s * 0.5 for s in m.encode(b"AB")]   # quieter, same tones
    assert m.decode(samples) == b"AB"
```

- [ ] **Step 2: Run → FAIL. Step 3: Implement** `modem.py`:

```python
"""AudioModem — pure-Python FSK soft-modem (spec §7). bytes <-> audio samples.

Each bit is SAMPLES_PER_BIT samples of a sine at MARK_HZ (1) or SPACE_HZ (0).
Decode compares per-bit correlation energy at the two tones (Goertzel-style). Clean
samples decode exactly; amplitude-invariant (compares relative energy). A real-radio
deployment needs a preamble/bit-sync — deferred; here encode/decode share the bit
grid so framing is implicit (the caller frames at the mesh layer)."""

from __future__ import annotations

import math

SAMPLE_RATE = 8000
MARK_HZ = 1200       # bit = 1
SPACE_HZ = 800       # bit = 0
SAMPLES_PER_BIT = 40  # 200 baud — generous for clean decode


def _tone(freq: float, n: int, phase0: float = 0.0) -> list[float]:
    w = 2 * math.pi * freq / SAMPLE_RATE
    return [math.sin(w * i + phase0) for i in range(n)]


def _energy(samples: list[float], freq: float) -> float:
    w = 2 * math.pi * freq / SAMPLE_RATE
    re = sum(s * math.cos(w * i) for i, s in enumerate(samples))
    im = sum(s * math.sin(w * i) for i, s in enumerate(samples))
    return re * re + im * im


class AudioModem:
    def __init__(self, samples_per_bit: int = SAMPLES_PER_BIT) -> None:
        self.spb = samples_per_bit

    def encode(self, data: bytes) -> list[float]:
        out: list[float] = []
        for byte in data:
            for bit in range(8):  # MSB-first
                hi = (byte >> (7 - bit)) & 1
                out.extend(_tone(MARK_HZ if hi else SPACE_HZ, self.spb))
        return out

    def decode(self, samples: list[float]) -> bytes:
        nbits = len(samples) // self.spb
        bits: list[int] = []
        for b in range(nbits):
            win = samples[b * self.spb:(b + 1) * self.spb]
            bits.append(1 if _energy(win, MARK_HZ) >= _energy(win, SPACE_HZ) else 0)
        out = bytearray()
        for i in range(0, len(bits) - 7, 8):
            byte = 0
            for k in range(8):
                byte = (byte << 1) | bits[i + k]
            out.append(byte)
        return bytes(out)
```

- [ ] **Step 4: Run → PASS. Step 5: Commit** `feat(glossa-mesh): AudioModem — pure-Python FSK soft-modem`.

---

## Task 2: `CarrierSenseMAC` + `FakeAudioMedium`

**Files:** Create `src/skchat/glossa_mesh/mac.py`. Test `tests/test_glossa_mac.py`.

The shared audio medium genuinely collides; the MAC is listen-before-talk.

- [ ] **Step 1: Failing test** — `tests/test_glossa_mac.py`:

```python
import asyncio

import pytest

from skchat.glossa_mesh.mac import CarrierSenseMAC, FakeAudioMedium


@pytest.mark.asyncio
async def test_two_transmits_collide_without_mac():
    med = FakeAudioMedium()
    # raw concurrent transmits overlap → medium marks a collision window
    await asyncio.gather(med.transmit_raw("a", [1.0] * 10),
                         med.transmit_raw("b", [1.0] * 10))
    assert med.had_collision is True


@pytest.mark.asyncio
async def test_mac_serializes_transmits_no_collision():
    med = FakeAudioMedium()
    mac = CarrierSenseMAC(med)
    await asyncio.gather(mac.send("a", [1.0] * 10),
                         mac.send("b", [1.0] * 10))
    assert med.had_collision is False        # MAC made them take turns
    assert med.transmissions == 2            # both still got through


def test_carrier_sense_reports_busy():
    med = FakeAudioMedium()
    assert med.is_busy() is False
    med._busy = True
    assert med.is_busy() is True
```

- [ ] **Step 2: Run → FAIL. Step 3: Implement** `mac.py`:

```python
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
            self.had_collision = True       # someone else is already transmitting
        self._busy = True
        try:
            await asyncio.sleep(0)           # yield — lets a concurrent tx overlap
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
        async with self._lock:              # acquire the channel
            await self._medium.transmit_raw(src, samples)
```

- [ ] **Step 4: Run → PASS. Step 5: Commit** `feat(glossa-mesh): carrier-sense MAC + FakeAudioMedium (collision sim)`.

---

## Task 3: `AudioMeshBus` — MeshBus over audio

**Files:** Create `src/skchat/glossa_mesh/audio_bus.py`. Test `tests/test_glossa_audio_bus.py`.

- [ ] **Step 1: Failing test** — `tests/test_glossa_audio_bus.py`:

```python
import asyncio

import pytest

from skchat.glossa_mesh.audio_bus import AudioMeshBus
from skchat.glossa_mesh.bus import MeshBus
from skchat.glossa_mesh.mac import FakeAudioMedium


def test_is_a_meshbus():
    assert issubclass(AudioMeshBus, MeshBus)


@pytest.mark.asyncio
async def test_frame_survives_the_acoustic_round_trip():
    med = FakeAudioMedium()
    a = AudioMeshBus("a", med)
    b = AudioMeshBus("b", med)
    got = []
    b.on_receive(lambda data, src: got.append((data, src)))
    await a.start(); await b.start()
    await a.broadcast(b"sk-over-audio")
    await asyncio.sleep(0.01)
    assert got == [(b"sk-over-audio", "a")]   # modulated, transmitted, demodulated


@pytest.mark.asyncio
async def test_sender_does_not_hear_itself():
    med = FakeAudioMedium()
    a = AudioMeshBus("a", med)
    got = []
    a.on_receive(lambda d, s: got.append(d))
    await a.start()
    await a.broadcast(b"x")
    await asyncio.sleep(0.01)
    assert got == []
```

- [ ] **Step 2: Run → FAIL. Step 3: Implement** `audio_bus.py`:

```python
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
```

> **NOTE for implementer:** if `MeshBus` doesn't declare `on_leave` (G3 added it as a
> concrete no-op on FakeBus), match whatever the real `MeshBus` ABC requires — keep
> `AudioMeshBus` a valid `MeshBus` subclass. Verify against `bus.py`.

- [ ] **Step 4: Run → PASS. Step 5: Commit** `feat(glossa-mesh): AudioMeshBus — SKGlossa over audio (modem + MAC)`.

---

## Task 4: Integration — a `GlossaMeshNode` meshes over audio

**Files:** Test `tests/test_glossa_audio_mesh.py`.

The payoff: the G3 node, unchanged, exchanges a real SKGlossa message over the
acoustic bus.

- [ ] **Step 1: Failing test** — `tests/test_glossa_audio_mesh.py`:

```python
import asyncio

import pytest

from skcomms.glossa import codec
from skcomms.glossa.codebook import default_codebook
from skcomms.glossa.handshake import CapabilityDescriptor
from skcomms.glossa.message import Message
from skchat.glossa_mesh.audio_bus import AudioMeshBus
from skchat.glossa_mesh.mac import FakeAudioMedium
from skchat.glossa_mesh.node import GlossaMeshNode


def _node(fqid, med):
    d = CapabilityDescriptor(fqid=fqid, model_tier="large", max_level=codec.L1_SCHEMA,
                             codebook_version=default_codebook().version, lexicon_version="")
    return GlossaMeshNode(descriptor=d, bus=AudioMeshBus(fqid, med),
                          codebook=default_codebook())


@pytest.mark.asyncio
async def test_skglossa_message_over_the_audio_mesh():
    med = FakeAudioMedium()
    a, b = _node("a@x.y", med), _node("b@x.y", med)
    inbox = []
    b.on_message(lambda fqid, m: inbox.append(m))
    await a.start(); await b.start()
    await a.announce(); await b.announce()
    await asyncio.sleep(0.02)
    await a.say(Message(intent="ack"))
    await asyncio.sleep(0.02)
    assert inbox == [Message(intent="ack")]    # SKGlossa, modulated to tones, decoded
```

- [ ] **Step 2: Run → PASS (it should, given Tasks 1–3).** If it fails, fix the minimal cause. **Step 3: Commit** `test(glossa-mesh): SKGlossa message meshes over the audio modem`.

---

## Final verification

- [ ] **Full mesh suite + whole skchat suite:**
Run: `~/.skenv/bin/python -m pytest tests/test_glossa_*.py -v && ~/.skenv/bin/python -m pytest tests/ -q`
Expected: all pass; no regressions.
- [ ] **Lint:** `~/.skenv/bin/ruff check src/skchat/glossa_mesh/ tests/test_glossa_modem.py tests/test_glossa_mac.py tests/test_glossa_audio_bus.py tests/test_glossa_audio_mesh.py` → no errors.

## What G5a delivers

The acoustic mesh tier: a pure-Python FSK soft-modem (bytes↔tones), a carrier-sense
MAC (so agents take turns on the shared audio channel instead of colliding), and an
`AudioMeshBus` that drops into the G3 mesh seam — so the *unchanged* `GlossaMeshNode`
can mesh SKGlossa **over audio**. CI-proven end-to-end (a message modulated to tones,
transmitted through a simulated shared medium, demodulated, decoded). Binding it to a
real LiveKit audio track (with a preamble/bit-sync for real-world timing) is the live
follow-on; the modem/MAC/bus proven here don't change.
