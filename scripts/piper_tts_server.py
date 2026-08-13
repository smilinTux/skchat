#!/usr/bin/env python3
"""Fast CPU TTS server (Piper) — OpenAI-compatible /v1/audio/speech.

Real-time-ish on CPU (~1-2 s/sentence), no GPU. Drop-in faster alternative to
the F5-TTS Arc-iGPU server (~113 s/sentence) for the skchat voice pipeline.

The ``voice`` field of the request is HONORED. It used to be accepted and then
silently ignored: one model was loaded at import and every request rendered with
it, whatever the caller asked for. The call path asks for ``af_heart``, which is
a *Kokoro* voice name, so after TTS was repointed off the dead Kokoro box onto
this server every reply came back in ``en_US-lessac-medium`` and Lumina's voice
audibly changed. Nothing failed, nothing logged, and the only symptom was Chef
saying she "sounds pretty deep". An ignored parameter is a silent lie; if a
voice cannot be served, say so instead.

Env:
  PIPER_MODEL      path to the default .onnx voice (default: en_US-amy-medium)
  PIPER_VOICE_DIR  where to resolve named voices from (default: the model's dir)
  PIPER_PORT       listen port (default 18797)
"""

from __future__ import annotations

import io
import logging
import os
import wave

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from piper import PiperVoice
from pydantic import BaseModel

MODEL = os.environ.get(
    "PIPER_MODEL",
    os.path.expanduser("~/.local/share/piper-voices/en_US-amy-medium.onnx"),
)
VOICE_DIR = os.environ.get("PIPER_VOICE_DIR", os.path.dirname(MODEL))
PORT = int(os.environ.get("PIPER_PORT", "18797"))

log = logging.getLogger("piper-tts")
app = FastAPI(title="Piper TTS (CPU)")

DEFAULT_VOICE = os.path.basename(MODEL).removesuffix(".onnx")
_voices: dict[str, PiperVoice] = {DEFAULT_VOICE: PiperVoice.load(MODEL)}


def _available() -> list[str]:
    """Voice names this server can actually render, newest scan each call."""
    try:
        found = {f.removesuffix(".onnx") for f in os.listdir(VOICE_DIR) if f.endswith(".onnx")}
    except OSError:
        found = set()
    return sorted(found | set(_voices))


def _resolve(name: str | None) -> PiperVoice:
    """Load (and cache) the requested voice, falling back to the default.

    Unknown names fall back rather than 500, because a dead call is worse than a
    wrong voice, but the fallback is LOGGED so it can never again be invisible.
    """
    want = (name or "").strip().removesuffix(".onnx")
    if not want or want == DEFAULT_VOICE:
        return _voices[DEFAULT_VOICE]
    if want in _voices:
        return _voices[want]
    path = os.path.join(VOICE_DIR, f"{want}.onnx")
    if os.path.exists(path):
        log.info("loading voice %s", want)
        _voices[want] = PiperVoice.load(path)
        return _voices[want]
    log.warning(
        "voice %r not available here (have: %s); rendering as %s",
        want,
        ",".join(_available()),
        DEFAULT_VOICE,
    )
    return _voices[DEFAULT_VOICE]


class SpeechReq(BaseModel):
    input: str | None = None
    text: str | None = None
    voice: str | None = None
    model: str | None = None


def _synth(text: str, voice: PiperVoice) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        try:
            voice.synthesize_wav(text, wf)
        except AttributeError:  # older API
            chunks = list(voice.synthesize(text))
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(chunks[0].sample_rate)
            for c in chunks:
                wf.writeframes(c.audio_int16_bytes)
    return buf.getvalue()


@app.get("/health")
def health():
    return {
        "ok": True,
        "engine": "piper",
        "device": "cpu",
        "model": DEFAULT_VOICE,
        "voices": _available(),
        "loaded": sorted(_voices),
    }


@app.get("/v1/voices")
def voices():
    return {"voices": _available(), "default": DEFAULT_VOICE}


@app.post("/v1/audio/speech")
@app.post("/audio/speech")
def speech(req: SpeechReq):
    text = (req.input or req.text or "").strip()
    if not text:
        return Response(content=b"", media_type="audio/wav")
    try:
        voice = _resolve(req.voice)
    except Exception as e:  # a corrupt/unloadable .onnx must not wedge the server
        raise HTTPException(status_code=500, detail=f"voice load failed: {e}") from e
    return Response(content=_synth(text, voice), media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
