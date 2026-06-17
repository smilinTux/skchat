"""AudioModem — pure-Python FSK soft-modem (spec §7). bytes <-> audio samples.

Each bit is SAMPLES_PER_BIT samples of a sine at MARK_HZ (1) or SPACE_HZ (0).
Decode compares per-bit correlation energy at the two tones (Goertzel-style). Clean
samples decode exactly; amplitude-invariant (compares relative energy). A real-radio
deployment needs a preamble/bit-sync — deferred; here encode/decode share the bit
grid so framing is implicit (the caller frames at the mesh layer)."""

from __future__ import annotations

import math

SAMPLE_RATE = 8000
MARK_HZ = 1200  # bit = 1
SPACE_HZ = 800  # bit = 0
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
            win = samples[b * self.spb : (b + 1) * self.spb]
            bits.append(1 if _energy(win, MARK_HZ) >= _energy(win, SPACE_HZ) else 0)
        out = bytearray()
        for i in range(0, len(bits) - 7, 8):
            byte = 0
            for k in range(8):
                byte = (byte << 1) | bits[i + k]
            out.append(byte)
        return bytes(out)
