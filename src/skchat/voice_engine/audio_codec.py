"""Pure audio helpers — PCM↔WAV and RMS. No network, no logging."""

from __future__ import annotations

import audioop
import io
import wave


def pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    """Wrap raw 16-bit signed little-endian PCM as a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def rms(pcm_bytes: bytes) -> int:
    """Root-mean-square amplitude of 16-bit PCM (0 == silence). 0 on error."""
    try:
        return audioop.rms(pcm_bytes, 2)
    except Exception:
        return 0


def detect_audio_format(data: bytes | None) -> str:
    """Sniff an audio container format from its leading magic bytes.

    Returns one of "wav", "webm", "ogg", "mp3", "m4a", or "" if unknown/too short.
    """
    if not data or len(data) < 4:
        return ""
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"\x1aE\xdf\xa3":
        return "webm"
    if data[:4] == b"OggS":
        return "ogg"
    if data[:3] == b"ID3" or data[:2] == b"\xff\xfb":
        return "mp3"
    if data[4:8] == b"ftyp":
        return "m4a"
    return ""
