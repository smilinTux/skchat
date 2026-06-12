import struct

import pytest

from skchat.voice_engine.config import VoiceConfig
from skchat.voice_engine.stt import STTClient, is_hallucination


def _tone(n, amp=8000):
    return struct.pack("<%dh" % n, *([amp, -amp] * (n // 2)))


def _silence(n):
    return struct.pack("<%dh" % n, *([0] * n))


def test_is_hallucination_matches_stock_phrases():
    assert is_hallucination("Thank you.")
    assert is_hallucination("thanks for watching!")
    assert is_hallucination("Thank you. Thank you, everyone.")  # repeated chain
    assert not is_hallucination("thank you for fixing the server")  # real, long


@pytest.mark.asyncio
async def test_vad_gate_drops_silence_without_calling_http():
    calls = []

    async def fake_post(url, wav_bytes):
        calls.append(url)
        return "should not happen"

    cfg = VoiceConfig.from_env(env={"SKVOICE_STT_MIN_RMS": "800"})
    stt = STTClient(cfg, _post=fake_post)
    out = await stt.transcribe(_silence(1600), vad=True)
    assert out == ""
    assert calls == []  # gated before HTTP


@pytest.mark.asyncio
async def test_loud_speech_calls_http_and_returns_text():
    async def fake_post(url, wav_bytes):
        return "hello there"

    cfg = VoiceConfig.from_env(env={"SKVOICE_STT_MIN_RMS": "800"})
    stt = STTClient(cfg, _post=fake_post)
    out = await stt.transcribe(_tone(1600), vad=True)
    assert out == "hello there"


@pytest.mark.asyncio
async def test_hallucination_dropped_even_when_loud():
    async def fake_post(url, wav_bytes):
        return "Thank you."

    cfg = VoiceConfig.from_env(env={})
    stt = STTClient(cfg, _post=fake_post)
    out = await stt.transcribe(_tone(1600), vad=True)
    assert out == ""


@pytest.mark.asyncio
async def test_vad_false_skips_gate_and_filter():
    async def fake_post(url, wav_bytes):
        return "Thank you."

    cfg = VoiceConfig.from_env(env={})
    stt = STTClient(cfg, _post=fake_post)
    # vad=False → plain transcribe, no gate, no hallucination filter
    out = await stt.transcribe(_silence(1600), vad=False)
    assert out == "Thank you."
