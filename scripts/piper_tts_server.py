#!/usr/bin/env python3
"""Fast CPU TTS server (Piper) — OpenAI-compatible /v1/audio/speech.

Real-time-ish on CPU (~1-2 s/sentence), no GPU. Drop-in faster alternative to
the F5-TTS Arc-iGPU server (~113 s/sentence) for the skchat voice pipeline.

Env:
  PIPER_MODEL  path to a .onnx voice (default: en_US-lessac-medium)
  PIPER_PORT   listen port (default 18797)
"""
from __future__ import annotations

import io
import os
import wave

from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import BaseModel
from piper import PiperVoice

MODEL = os.environ.get(
    "PIPER_MODEL",
    os.path.expanduser("~/.local/share/piper-voices/en_US-lessac-medium.onnx"),
)
PORT = int(os.environ.get("PIPER_PORT", "18797"))

app = FastAPI(title="Piper TTS (CPU)")
_voice = PiperVoice.load(MODEL)


class SpeechReq(BaseModel):
    input: str | None = None
    text: str | None = None
    voice: str | None = None
    model: str | None = None


def _synth(text: str) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        try:
            _voice.synthesize_wav(text, wf)
        except AttributeError:  # older API
            chunks = list(_voice.synthesize(text))
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(chunks[0].sample_rate)
            for c in chunks:
                wf.writeframes(c.audio_int16_bytes)
    return buf.getvalue()


@app.get("/health")
def health():
    return {"ok": True, "engine": "piper", "device": "cpu", "model": os.path.basename(MODEL)}


@app.get("/v1/voices")
def voices():
    return {"voices": [os.path.basename(MODEL).replace(".onnx", "")]}


@app.post("/v1/audio/speech")
@app.post("/audio/speech")
def speech(req: SpeechReq):
    text = (req.input or req.text or "").strip()
    if not text:
        return Response(content=b"", media_type="audio/wav")
    return Response(content=_synth(text), media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
