# ============================================================================
# Streaming endpoint — added 2026-04-28 to enable voice-pace TTS for Lumina.
# VoxCPM 2.0+ exposes model.generate_streaming() that yields ~80ms PCM chunks.
# This endpoint wraps it as a FastAPI StreamingResponse emitting raw int16 PCM
# (or WAV with indeterminate length header). First-chunk TTFB ~300-500ms vs
# 2-4s for the batch endpoint.
# ============================================================================
from fastapi.responses import StreamingResponse
import struct as _struct


def _streaming_wav_header(sample_rate: int, num_channels: int = 1, bits: int = 16) -> bytes:
    """44-byte WAV header with 0xFFFFFFFF data size (unknown length, streaming)."""
    byte_rate = sample_rate * num_channels * (bits // 8)
    block_align = num_channels * (bits // 8)
    return (
        b"RIFF" + b"\xff\xff\xff\xff"
        + b"WAVEfmt " + _struct.pack("<I", 16)
        + _struct.pack("<H", 1)
        + _struct.pack("<H", num_channels)
        + _struct.pack("<I", sample_rate)
        + _struct.pack("<I", byte_rate)
        + _struct.pack("<H", block_align)
        + _struct.pack("<H", bits)
        + b"data" + b"\xff\xff\xff\xff"
    )


def _stream_chunks(req):
    """Yield int16 PCM bytes from VoxCPM streaming generation."""
    model = get_model()
    ref = resolve_voice(req.voice)
    kwargs = dict(
        text=req.input,
        cfg_value=req.cfg_value if req.cfg_value is not None else CFG_VALUE,
        inference_timesteps=req.inference_timesteps or INFERENCE_TIMESTEPS,
    )
    if ref is not None:
        kwargs["reference_wav_path"] = str(ref)
        prompt_text = req.prompt_text
        if not prompt_text:
            sidecar = Path(str(ref)).with_suffix(".txt")
            if sidecar.is_file():
                prompt_text = sidecar.read_text(encoding="utf-8").strip() or None
        if prompt_text:
            kwargs["prompt_wav_path"] = str(ref)
            kwargs["prompt_text"] = prompt_text

    fmt = (req.response_format or "wav").lower()
    if fmt == "wav":
        yield _streaming_wav_header(_sample_rate, 1, 16)

    chunk_count = 0
    bytes_total = 0
    t0 = time.perf_counter()
    for chunk in model.generate_streaming(**kwargs):
        if isinstance(chunk, torch.Tensor):
            chunk = chunk.detach().cpu().numpy()
        chunk = np.asarray(chunk, dtype=np.float32).squeeze()
        pcm = (np.clip(chunk, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
        chunk_count += 1
        bytes_total += len(pcm)
        yield pcm
    elapsed = time.perf_counter() - t0
    print(
        f"[voxcpm-stream] voice={req.voice or DEFAULT_VOICE!r} chars={len(req.input)} "
        f"chunks={chunk_count} bytes={bytes_total} wall_s={elapsed:.2f}",
        flush=True,
    )


@app.post("/v1/audio/speech/stream")
@app.post("/audio/speech/stream")
def speech_stream(req: SpeechRequest):
    fmt = (req.response_format or "wav").lower()
    media_type = "audio/wav" if fmt == "wav" else "audio/L16; rate=" + str(_sample_rate)
    return StreamingResponse(_stream_chunks(req), media_type=media_type)
