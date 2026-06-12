"""TTSClient — OpenAI-compatible /audio/speech. Batch returns WAV bytes;
stream() yields raw int16 PCM chunks from the /audio/speech/stream endpoint
(lumina-call's low-latency path).
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Awaitable, Callable

import httpx

from skchat.voice_engine.config import VoiceConfig

log = logging.getLogger("skchat.voice_engine.tts")


def stream_url_for(tts_url: str) -> str:
    """Derive the streaming endpoint from the batch one (matches lumina-call)."""
    base = tts_url.rsplit("/audio/speech", 1)[0]
    return f"{base}/audio/speech/stream"


PostFn = Callable[[str, dict], Awaitable[bytes]]
StreamFn = Callable[[str, dict], AsyncIterator[bytes]]


class TTSClient:
    def __init__(self, cfg: VoiceConfig, _post: PostFn | None = None,
                 _stream: StreamFn | None = None):
        self.cfg = cfg
        self._post = _post or self._http_post
        self._stream = _stream or self._http_stream

    async def _http_post(self, url: str, payload: dict) -> bytes:
        async with httpx.AsyncClient(timeout=60.0) as http:
            r = await http.post(url, json=payload)
            r.raise_for_status()
            return r.content

    async def _http_stream(self, url: str, payload: dict) -> AsyncIterator[bytes]:
        async with httpx.AsyncClient(timeout=60.0) as http:
            async with http.stream("POST", url, json=payload) as r:
                r.raise_for_status()
                async for chunk in r.aiter_bytes():
                    if chunk:
                        yield chunk

    async def synthesize(self, text: str, *, voice: str) -> bytes:
        """Full WAV bytes, or b'' on failure."""
        payload = {"model": "tts-1", "input": text, "voice": voice,
                   "response_format": "wav"}
        try:
            return await self._post(self.cfg.tts_url, payload)
        except Exception as e:
            log.error("TTS failed: %s", e)
            return b""

    async def stream(self, text: str, *, voice: str) -> AsyncIterator[bytes]:
        """Yield raw int16 PCM chunks from the streaming endpoint."""
        payload = {"model": "tts-1", "input": text, "voice": voice,
                   "response_format": "pcm"}
        url = stream_url_for(self.cfg.tts_url)
        try:
            async for chunk in self._stream(url, payload):
                yield chunk
        except Exception as e:
            log.error("TTS stream failed: %s", e)
