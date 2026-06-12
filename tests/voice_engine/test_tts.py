import pytest

from skchat.voice_engine.config import VoiceConfig
from skchat.voice_engine.tts import TTSClient, stream_url_for


def test_stream_url_derivation():
    assert stream_url_for("http://localhost:15091/audio/speech") == \
        "http://localhost:15091/audio/speech/stream"


@pytest.mark.asyncio
async def test_synthesize_posts_and_returns_bytes():
    seen = {}

    async def fake_post(url, payload):
        seen["url"] = url
        seen["payload"] = payload
        return b"RIFF....WAVE"

    cfg = VoiceConfig.from_env(env={})
    tts = TTSClient(cfg, _post=fake_post)
    out = await tts.synthesize("hello", voice="lumina")
    assert out == b"RIFF....WAVE"
    assert seen["url"] == cfg.tts_url
    assert seen["payload"]["input"] == "hello"
    assert seen["payload"]["voice"] == "lumina"
    assert seen["payload"]["response_format"] == "wav"


@pytest.mark.asyncio
async def test_synthesize_returns_empty_on_error():
    async def fake_post(url, payload):
        raise RuntimeError("tts down")

    cfg = VoiceConfig.from_env(env={})
    tts = TTSClient(cfg, _post=fake_post)
    assert await tts.synthesize("hi", voice="lumina") == b""


@pytest.mark.asyncio
async def test_stream_yields_pcm_chunks_and_uses_stream_url():
    seen = {}

    async def fake_stream(url, payload):
        seen["url"] = url
        for chunk in [b"\x01\x02", b"\x03\x04"]:
            yield chunk

    cfg = VoiceConfig.from_env(env={})
    tts = TTSClient(cfg, _stream=fake_stream)
    chunks = [c async for c in tts.stream("hi", voice="lumina")]
    assert chunks == [b"\x01\x02", b"\x03\x04"]
    assert seen["url"] == "http://localhost:15091/audio/speech/stream"
