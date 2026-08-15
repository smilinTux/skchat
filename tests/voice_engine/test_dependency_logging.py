"""A failing dependency must say WHICH one and WHY.

During an outage on 2026-08-13 the STT host went away mid-call. She heard Chef
fine (VAD opened on real speech) and never answered, and the only trace was
this, five times:

    ERROR skchat.voice_engine.stt: STT failed:

`str()` on an httpx connect timeout is EMPTY, so the message named no service,
no URL and no failure kind. It announced that something broke and refused to
say what, and it was skimmed past while the symptom got blamed on a wedged
segmenter for an hour.
"""

import httpx
import pytest

from skchat.voice_engine.config import VoiceConfig
from skchat.voice_engine.stt import STTClient
from skchat.voice_engine.tts import TTSClient

CFG = VoiceConfig.from_env(
    env={
        "SKVOICE_STT_URL": "http://stt.invalid:18794/v1/audio/transcriptions",
        "SKVOICE_TTS_URL": "http://tts.invalid:18796/audio/speech",
    }
)


@pytest.mark.asyncio
async def test_stt_failure_names_the_service_url_and_exception(caplog):
    async def _boom(url, wav):
        # str() is deliberately empty, exactly like a real connect timeout.
        raise httpx.ConnectTimeout("")

    client = STTClient(CFG, _post=_boom)
    with caplog.at_level("ERROR"):
        assert await client.transcribe(b"\x00\x00" * 8000) == ""

    msg = caplog.text
    assert "ConnectTimeout" in msg, "the failure KIND must survive an empty str(e)"
    assert "stt.invalid" in msg, "the message must name which service was unreachable"
    assert "stay silent" in msg, "it must say what the user will actually observe"


@pytest.mark.asyncio
async def test_tts_failure_names_the_service_and_voice(caplog):
    async def _boom(url, payload):
        raise httpx.ConnectTimeout("")

    client = TTSClient(CFG, _post=_boom)
    with caplog.at_level("ERROR"):
        assert await client.synthesize("hello", voice="lumina") == b""

    msg = caplog.text
    assert "ConnectTimeout" in msg
    assert "tts.invalid" in msg
    assert "lumina" in msg


@pytest.mark.asyncio
async def test_an_empty_exception_never_produces_a_bare_message(caplog):
    """The exact regression: 'STT failed:' with nothing after the colon."""

    async def _boom(url, wav):
        raise httpx.ConnectTimeout("")

    with caplog.at_level("ERROR"):
        await STTClient(CFG, _post=_boom).transcribe(b"\x00\x00" * 8000)

    for line in caplog.text.splitlines():
        if "STT failed" in line:
            assert not line.rstrip().endswith("STT failed:"), (
                f"bare, information-free failure line: {line!r}"
            )
