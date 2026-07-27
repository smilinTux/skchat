"""STTClient — faster-whisper transcription with optional VAD + hallucination
filtering. The energy gate and stock-phrase filter (ported from lumina-call)
keep whisper from inventing words on near-silent audio; enable via vad=True.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

import httpx

from skchat.voice_engine.audio_codec import pcm_to_wav, rms
from skchat.voice_engine.config import VoiceConfig

log = logging.getLogger("skchat.voice_engine.stt")

# Whisper's well-known low-SNR hallucinations (YouTube corpus). Match on the
# normalized FULL string (equals), not substring — a real reply may contain
# "thank you".
_HALLUCINATIONS = frozenset(
    s.lower()
    for s in (
        "thank you",
        "thank you.",
        "thanks.",
        "thank you very much.",
        "thank you very much",
        "thank you so much.",
        "thanks for watching",
        "thanks for watching!",
        "thank you for watching",
        "thank you for watching.",
        "bye.",
        "bye bye.",
        "goodbye.",
        "good bye.",
        "okay.",
        "ok.",
        "you",
        "you.",
        "yeah.",
        "uh huh.",
        "mhm.",
        "mhmm.",
        "hmm.",
        ".",
        "...",
        "..",
        "subscribe.",
        "like and subscribe.",
        "please subscribe.",
        "thanks!",
        "thank you!",
        "thanks for listening.",
        "i'll see you later.",
        "see you later.",
    )
)


def is_hallucination(text: str) -> bool:
    """True if `text` is a known whisper stock-phrase hallucination."""
    norm = text.lower().rstrip("!?")
    if norm in _HALLUCINATIONS:
        return True
    # Repeated "thank you" chain on a short clip ("Thank you. Thank you.").
    if text.lower().count("thank you") >= 2 and len(text) < 120:
        return True
    return False


PostFn = Callable[[str, bytes], Awaitable[str]]


class STTClient:
    def __init__(self, cfg: VoiceConfig, _post: PostFn | None = None, _post_file=None):
        self.cfg = cfg
        self._post = _post or self._http_post
        self._post_file = _post_file or self._http_post_file

    async def _http_post(self, url: str, wav_bytes: bytes) -> str:
        async with httpx.AsyncClient(timeout=30.0) as client:
            files = {"file": ("speech.wav", wav_bytes, "audio/wav")}
            r = await client.post(url, files=files, data={"model": "whisper-1"})
            r.raise_for_status()
            return (r.json().get("text") or "").strip()

    async def _http_post_file(
        self, url: str, audio: bytes, *, filename: str, content_type: str
    ) -> str:
        async with httpx.AsyncClient(timeout=30.0) as client:
            files = {"file": (filename, audio, content_type)}
            r = await client.post(url, files=files, data={"model": "whisper-1"})
            r.raise_for_status()
            return (r.json().get("text") or "").strip()

    async def transcribe_upload(
        self, audio: bytes, *, filename: str = "speech.wav",
        content_type: str = "audio/wav",
    ) -> str:
        """Transcribe an ALREADY-ENCODED audio file (wav/webm/mp3/m4a) by posting
        it straight to the whisper endpoint.

        The app's mic records webm/opus (browser) or wav (native); faster-whisper
        accepts those directly, so unlike ``transcribe`` this does NOT expect raw
        16 kHz PCM. RAISES on a transport/HTTP error so the caller can graceful-
        degrade (the /api/v1/transcribe route maps that to 503 -> the app disables
        the mic). An empty transcript (genuine silence) returns "".
        """
        return await self._post_file(
            self.cfg.stt_url, audio, filename=filename, content_type=content_type
        )

    async def transcribe(self, pcm16k_mono: bytes, *, vad: bool = False) -> str:
        """16 kHz mono PCM → transcript. vad=True applies the energy gate +
        hallucination filter; vad=False is a plain batch transcription."""
        if vad and rms(pcm16k_mono) < self.cfg.stt_min_rms:
            log.info("stt: dropping (rms < %d, likely silence)", self.cfg.stt_min_rms)
            return ""
        wav = pcm_to_wav(pcm16k_mono, sample_rate=16000, channels=1)
        try:
            text = await self._post(self.cfg.stt_url, wav)
        except Exception as e:
            log.error("STT failed: %s", e)
            return ""
        if vad and text and is_hallucination(text):
            log.info("stt: dropping hallucination %r", text)
            return ""
        return text
