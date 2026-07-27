"""Tests for the STT transcribe path: STTClient.transcribe_upload + the
POST /api/v1/transcribe route (unified-conversations Phase 4 voice input).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy
from skchat.voice_engine.config import VoiceConfig
from skchat.voice_engine.stt import STTClient


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(daemon_proxy.router)
    return TestClient(app)


# -- STTClient.transcribe_upload -------------------------------------------
async def test_transcribe_upload_posts_encoded_file_verbatim():
    captured = {}

    async def fake_post_file(url, audio, *, filename, content_type):
        captured.update(url=url, audio=audio, filename=filename, content_type=content_type)
        return "  transcribed text  "

    cfg = VoiceConfig.from_env()
    stt = STTClient(cfg, _post_file=fake_post_file)
    out = await stt.transcribe_upload(
        b"WEBMDATA", filename="speech.webm", content_type="audio/webm"
    )
    assert out == "  transcribed text  "  # returns the poster's result as-is
    assert captured["audio"] == b"WEBMDATA"  # encoded bytes posted unchanged
    assert captured["content_type"] == "audio/webm"
    assert captured["filename"] == "speech.webm"
    assert captured["url"] == cfg.stt_url


async def test_transcribe_upload_propagates_transport_error():
    async def boom(url, audio, *, filename, content_type):
        raise ConnectionError("whisper down")

    stt = STTClient(VoiceConfig.from_env(), _post_file=boom)
    with pytest.raises(ConnectionError):
        await stt.transcribe_upload(b"A")  # caller graceful-degrades, not swallowed


# -- POST /api/v1/transcribe -----------------------------------------------
def _patch_upload(monkeypatch, fn):
    monkeypatch.setattr("skchat.voice_engine.stt.STTClient.transcribe_upload", fn)


def test_transcribe_route_returns_transcript(client, monkeypatch):
    async def fake(self, audio, *, filename="speech.wav", content_type="audio/wav"):
        assert audio == b"FAKEAUDIO" and content_type == "audio/webm"
        assert filename == "speech.webm"  # ext derived from content-type
        return "hello world"

    _patch_upload(monkeypatch, fake)
    r = client.post(
        "/api/v1/transcribe", content=b"FAKEAUDIO", headers={"content-type": "audio/webm"}
    )
    assert r.status_code == 200
    assert r.json() == {"transcript": "hello world"}


def test_transcribe_route_400_on_empty_body(client):
    r = client.post("/api/v1/transcribe", content=b"", headers={"content-type": "audio/webm"})
    assert r.status_code == 400


def test_transcribe_route_413_on_oversized(client):
    big = b"x" * (daemon_proxy._MAX_STT_BYTES + 1)
    r = client.post("/api/v1/transcribe", content=big, headers={"content-type": "audio/wav"})
    assert r.status_code == 413


def test_transcribe_route_503_when_stt_unreachable(client, monkeypatch):
    async def boom(self, audio, **k):
        raise ConnectionError("whisper down")

    _patch_upload(monkeypatch, boom)
    r = client.post("/api/v1/transcribe", content=b"AUDIO", headers={"content-type": "audio/wav"})
    assert r.status_code == 503


def test_transcribe_route_empty_transcript_is_ok(client, monkeypatch):
    # Genuine silence -> "" transcript, still a 200 (not an error).
    async def silent(self, audio, **k):
        return ""

    _patch_upload(monkeypatch, silent)
    r = client.post(
        "/api/v1/transcribe", content=b"SILENCE", headers={"content-type": "audio/wav"}
    )
    assert r.status_code == 200 and r.json() == {"transcript": ""}
