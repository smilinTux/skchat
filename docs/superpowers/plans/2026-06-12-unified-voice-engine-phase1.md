# Unified Voice Engine — Phase 1 (Extract the Engine) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `skchat.voice_engine` — a transport-free, unit-tested library that owns the STT→LLM→TTS→persona→memory core shared by the web chat and the LiveKit call.

**Architecture:** Six focused modules under `src/skchat/voice_engine/`, each one responsibility, each with a small client class constructed from a single `VoiceConfig`. The engine is the *superset* of today's two pipelines: OpenAI-compat LLM with batch + streaming + primary→fallback, batch + streaming TTS, STT with opt-in VAD/hallucination filtering, soul+FEB persona, and a skmemory bridge. No transport, no FastAPI, no LiveKit here — those come in Phases 2-3.

**Tech Stack:** Python 3.12, `httpx` (async HTTP), `pytest` + `pytest-asyncio`, `wave`/`audioop` (stdlib audio), `skmemory` (SDK). Live endpoints used in tests: whisper STT `http://skworld-100:18794`, LLM proxy `http://localhost:18783`, qwen3.6-abliterated `http://192.168.0.100:8082`, kokoro-proxy TTS `http://localhost:15091`.

**Spec:** `docs/superpowers/specs/2026-06-12-unified-voice-engine-design.md`

**Conventions:**
- Repo root: `/home/cbrd21/clawd/skcapstone-repos/skchat`. Run all commands from there.
- Tests run with `~/.skenv/bin/python -m pytest` (the project venv). `pythonpath=src` is already set in `pyproject.toml`, so `import skchat.voice_engine...` works in tests.
- Branch is already `feat/unified-voice-engine`.
- Network tests that hit live endpoints are marked `@pytest.mark.live` and SKIPPED by default (see Task 1) so CI/offline runs stay green; run them explicitly with `-m live` when the boxes are up.
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## File Structure (created in this phase)

| File | Responsibility |
|---|---|
| `src/skchat/voice_engine/__init__.py` | Package exports: `VoiceConfig`, `STTClient`, `LLMClient`, `TTSClient`, `MemoryBridge`, `PersonaBuilder`, `Msg` |
| `src/skchat/voice_engine/config.py` | `VoiceConfig` dataclass — single env schema (models, URLs, voices, VAD knobs) |
| `src/skchat/voice_engine/audio_codec.py` | `pcm_to_wav()`, `rms()` — pure stdlib audio helpers (no I/O) |
| `src/skchat/voice_engine/stt.py` | `STTClient.transcribe(pcm, vad=…)` — whisper POST + opt-in VAD/hallucination filter |
| `src/skchat/voice_engine/llm.py` | `LLMClient.reply()` / `.stream()` — OpenAI-compat batch + streaming + primary→fallback |
| `src/skchat/voice_engine/tts.py` | `TTSClient.synthesize()` / `.stream()` — batch WAV + streaming PCM |
| `src/skchat/voice_engine/memory.py` | `MemoryBridge.search()` / `.snapshot()` — skmemory bridge |
| `src/skchat/voice_engine/persona.py` | `PersonaBuilder.build(agent, mode)` — soul/FEB/ritual → system prompt |
| `tests/voice_engine/test_*.py` | One test module per engine module |

Each module is < 150 lines. Transports (Phases 2-3) import this package; nothing here imports a transport.

---

## Task 1: Package skeleton + test marker

**Files:**
- Create: `src/skchat/voice_engine/__init__.py`
- Create: `tests/voice_engine/__init__.py`
- Create: `tests/voice_engine/test_smoke.py`
- Modify: `pyproject.toml` (register the `live` marker)

- [ ] **Step 1: Create the empty package**

Create `src/skchat/voice_engine/__init__.py` with a docstring only (exports get added as modules land):

```python
"""skchat.voice_engine — the shared STT→LLM→TTS conversational core.

Transport-free. The WebSocket (web chat) and LiveKit (call) transports both
construct these clients from a single VoiceConfig. See
docs/superpowers/specs/2026-06-12-unified-voice-engine-design.md.
"""
```

- [ ] **Step 2: Create the test package**

Create `tests/voice_engine/__init__.py` as an empty file.

- [ ] **Step 3: Write a smoke test**

Create `tests/voice_engine/test_smoke.py`:

```python
def test_package_imports():
    import skchat.voice_engine  # noqa: F401
```

- [ ] **Step 4: Register the `live` marker** so live tests are opt-in

In `pyproject.toml`, find the `[tool.pytest.ini_options]` block and add a `markers` entry (and default `-m "not live"`) right after `pythonpath = ["src"]`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
markers = [
    "live: test hits live model endpoints (skworld-100/.100/localhost); run with -m live",
]
addopts = "-m 'not live'"
```

- [ ] **Step 5: Run the smoke test**

Run: `cd /home/cbrd21/clawd/skcapstone-repos/skchat && ~/.skenv/bin/python -m pytest tests/voice_engine/test_smoke.py -v`
Expected: PASS (1 passed).

- [ ] **Step 6: Commit**

```bash
git add src/skchat/voice_engine/__init__.py tests/voice_engine/ pyproject.toml
git commit -m "voice_engine: package skeleton + live test marker

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: VoiceConfig (the single config surface)

**Files:**
- Create: `src/skchat/voice_engine/config.py`
- Test: `tests/voice_engine/test_config.py`

This collapses both `skvoice.env` and the lumina-call env sprawl into one schema. Defaults reflect the **current working** endpoints (post the 2026-06-12 backend fix): local haiku proxy primary, qwen3.6-abliterated fallback, kokoro-proxy TTS, whisper STT.

- [ ] **Step 1: Write the failing test**

Create `tests/voice_engine/test_config.py`:

```python
import os
from skchat.voice_engine.config import VoiceConfig


def test_defaults_reflect_working_endpoints():
    cfg = VoiceConfig.from_env(env={})
    assert cfg.llm_url == "http://localhost:18783/v1/chat/completions"
    assert cfg.model == "claude-haiku-4-5"
    assert cfg.fallback_url == "http://192.168.0.100:8082/v1/chat/completions"
    assert cfg.fallback_model == "qwen3.6-27b-abliterated"
    assert cfg.tts_url == "http://localhost:15091/audio/speech"
    assert cfg.tts_voice == "lumina"
    assert cfg.stt_url == "http://skworld-100:18794/v1/audio/transcriptions"
    assert cfg.stt_min_rms == 800
    assert cfg.max_tokens == 200


def test_env_overrides_take_precedence():
    cfg = VoiceConfig.from_env(env={
        "SKVOICE_MODEL": "claude-opus-4-7",
        "SKVOICE_STT_MIN_RMS": "350",
    })
    assert cfg.model == "claude-opus-4-7"
    assert cfg.stt_min_rms == 350


def test_from_env_reads_os_environ_by_default(monkeypatch):
    monkeypatch.setenv("SKVOICE_TTS_VOICE", "af_heart")
    cfg = VoiceConfig.from_env()
    assert cfg.tts_voice == "af_heart"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/voice_engine/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'skchat.voice_engine.config'`.

- [ ] **Step 3: Write the implementation**

Create `src/skchat/voice_engine/config.py`:

```python
"""VoiceConfig — the single environment schema for the voice engine.

One place to set models, endpoints, voice, and VAD knobs. Both transports
construct their clients from this. Defaults match the live, working endpoints
as of 2026-06-12 (local haiku proxy + qwen3.6-abliterated fallback + kokoro
TTS proxy + whisper STT).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class VoiceConfig:
    # LLM (OpenAI-compatible /v1/chat/completions on both legs)
    llm_url: str
    model: str
    fallback_url: str
    fallback_model: str
    max_tokens: int
    # STT (faster-whisper)
    stt_url: str
    stt_min_rms: int
    # TTS (OpenAI-compatible /audio/speech)
    tts_url: str
    tts_voice: str
    # identity
    agent: str

    @staticmethod
    def from_env(env: Mapping[str, str] | None = None) -> "VoiceConfig":
        e = os.environ if env is None else env

        def g(key: str, default: str) -> str:
            return e.get(key, default)

        return VoiceConfig(
            llm_url=g("SKVOICE_LLM_URL", "http://localhost:18783/v1/chat/completions"),
            model=g("SKVOICE_MODEL", "claude-haiku-4-5"),
            fallback_url=g("SKVOICE_FALLBACK_URL", "http://192.168.0.100:8082/v1/chat/completions"),
            fallback_model=g("SKVOICE_FALLBACK_MODEL", "qwen3.6-27b-abliterated"),
            max_tokens=int(g("SKVOICE_MAX_TOKENS", "200")),
            stt_url=g("SKVOICE_STT_URL", "http://skworld-100:18794/v1/audio/transcriptions"),
            stt_min_rms=int(g("SKVOICE_STT_MIN_RMS", "800")),
            tts_url=g("SKVOICE_TTS_URL", "http://localhost:15091/audio/speech"),
            tts_voice=g("SKVOICE_TTS_VOICE", "lumina"),
            agent=g("SKVOICE_AGENT", "lumina"),
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/voice_engine/test_config.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/skchat/voice_engine/config.py tests/voice_engine/test_config.py
git commit -m "voice_engine: VoiceConfig — single env schema, working-endpoint defaults

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: audio_codec (pure helpers)

**Files:**
- Create: `src/skchat/voice_engine/audio_codec.py`
- Test: `tests/voice_engine/test_audio_codec.py`

Pull the PCM/WAV + RMS helpers out so both STT (gating) and transports (framing) share them. Pure functions, no I/O — trivially testable.

- [ ] **Step 1: Write the failing test**

Create `tests/voice_engine/test_audio_codec.py`:

```python
import wave
import io
import struct
from skchat.voice_engine.audio_codec import pcm_to_wav, rms


def _silence(n_samples: int) -> bytes:
    return struct.pack("<%dh" % n_samples, *([0] * n_samples))


def _tone(n_samples: int, amp: int = 8000) -> bytes:
    return struct.pack("<%dh" % n_samples, *([amp, -amp] * (n_samples // 2)))


def test_pcm_to_wav_roundtrips_header_and_frames():
    pcm = _tone(1600)  # 0.1s @ 16k
    wav = pcm_to_wav(pcm, sample_rate=16000, channels=1)
    with wave.open(io.BytesIO(wav), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getframerate() == 16000
        assert wf.getsampwidth() == 2
        assert wf.readframes(wf.getnframes()) == pcm


def test_rms_zero_for_silence():
    assert rms(_silence(1600)) == 0


def test_rms_high_for_tone():
    assert rms(_tone(1600, amp=8000)) > 5000
```

- [ ] **Step 2: Run it to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/voice_engine/test_audio_codec.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'skchat.voice_engine.audio_codec'`.

- [ ] **Step 3: Write the implementation**

Create `src/skchat/voice_engine/audio_codec.py`:

```python
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/voice_engine/test_audio_codec.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/skchat/voice_engine/audio_codec.py tests/voice_engine/test_audio_codec.py
git commit -m "voice_engine: audio_codec — pcm_to_wav + rms helpers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: STTClient (transcribe + opt-in VAD/hallucination filter)

**Files:**
- Create: `src/skchat/voice_engine/stt.py`
- Test: `tests/voice_engine/test_stt.py`

Superset of both pipelines: plain transcribe (skvoice) + the VAD energy gate and whisper-hallucination filter (lumina-call), behind a `vad` flag. The HTTP POST is injected so tests don't need the network.

- [ ] **Step 1: Write the failing test**

Create `tests/voice_engine/test_stt.py`:

```python
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/voice_engine/test_stt.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'skchat.voice_engine.stt'`.

- [ ] **Step 3: Write the implementation**

Create `src/skchat/voice_engine/stt.py`:

```python
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
_HALLUCINATIONS = frozenset(s.lower() for s in (
    "thank you", "thank you.", "thanks.", "thank you very much.",
    "thank you very much", "thank you so much.", "thanks for watching",
    "thanks for watching!", "thank you for watching", "thank you for watching.",
    "bye.", "bye bye.", "goodbye.", "good bye.", "okay.", "ok.",
    "you", "you.", "yeah.", "uh huh.", "mhm.", "mhmm.", "hmm.",
    ".", "...", "..", "subscribe.", "like and subscribe.",
    "please subscribe.", "thanks!", "thank you!", "thanks for listening.",
    "i'll see you later.", "see you later.",
))


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
    def __init__(self, cfg: VoiceConfig, _post: PostFn | None = None):
        self.cfg = cfg
        self._post = _post or self._http_post

    async def _http_post(self, url: str, wav_bytes: bytes) -> str:
        async with httpx.AsyncClient(timeout=30.0) as client:
            files = {"file": ("speech.wav", wav_bytes, "audio/wav")}
            r = await client.post(url, files=files, data={"model": "whisper-1"})
            r.raise_for_status()
            return (r.json().get("text") or "").strip()

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
```

- [ ] **Step 4: Run to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/voice_engine/test_stt.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/skchat/voice_engine/stt.py tests/voice_engine/test_stt.py
git commit -m "voice_engine: STTClient — transcribe + opt-in VAD/hallucination filter

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: LLMClient (batch + streaming + primary→fallback)

**Files:**
- Create: `src/skchat/voice_engine/llm.py`
- Test: `tests/voice_engine/test_llm.py`

Generalizes the OpenAI-compat client just landed in `skvoice/llm.py` (the SDK-retirement fix) plus lumina-call's streaming path. `reply()` is batch with primary→fallback; `stream()` yields token deltas from the primary. HTTP is injected for tests.

- [ ] **Step 1: Write the failing test**

Create `tests/voice_engine/test_llm.py`:

```python
import pytest
from skchat.voice_engine.config import VoiceConfig
from skchat.voice_engine.llm import LLMClient, strip_formatting


def test_strip_formatting_removes_markdown_and_emoji():
    assert strip_formatting("**hi** _there_") == "hi there"
    assert strip_formatting("Hello 😊 world").strip() == "Hello  world".strip()


@pytest.mark.asyncio
async def test_reply_uses_primary_when_it_succeeds():
    seen = []

    async def fake_chat(url, model, messages):
        seen.append((url, model))
        return "primary says hi"

    cfg = VoiceConfig.from_env(env={})
    llm = LLMClient(cfg, _chat=fake_chat)
    out = await llm.reply([{"role": "user", "content": "hi"}])
    assert out == "primary says hi"
    assert seen[0] == (cfg.llm_url, cfg.model)


@pytest.mark.asyncio
async def test_reply_falls_back_on_primary_error():
    calls = []

    async def fake_chat(url, model, messages):
        calls.append(url)
        if url == cfg.llm_url:
            raise RuntimeError("429 rate limit")
        return "fallback says hi"

    cfg = VoiceConfig.from_env(env={})
    llm = LLMClient(cfg, _chat=fake_chat)
    out = await llm.reply([{"role": "user", "content": "hi"}])
    assert out == "fallback says hi"
    assert calls == [cfg.llm_url, cfg.fallback_url]


@pytest.mark.asyncio
async def test_reply_falls_back_on_empty_primary():
    async def fake_chat(url, model, messages):
        return "" if url == cfg.llm_url else "fallback text"

    cfg = VoiceConfig.from_env(env={})
    llm = LLMClient(cfg, _chat=fake_chat)
    out = await llm.reply([{"role": "user", "content": "hi"}])
    assert out == "fallback text"


@pytest.mark.asyncio
async def test_reply_returns_safe_message_when_both_fail():
    async def fake_chat(url, model, messages):
        raise RuntimeError("down")

    cfg = VoiceConfig.from_env(env={})
    llm = LLMClient(cfg, _chat=fake_chat)
    out = await llm.reply([{"role": "user", "content": "hi"}])
    assert "trouble connecting" in out.lower()


@pytest.mark.asyncio
async def test_stream_yields_deltas():
    async def fake_stream(url, model, messages):
        for tok in ["Hel", "lo ", "there"]:
            yield tok

    cfg = VoiceConfig.from_env(env={})
    llm = LLMClient(cfg, _stream=fake_stream)
    got = [t async for t in llm.stream([{"role": "user", "content": "hi"}])]
    assert "".join(got) == "Hello there"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/voice_engine/test_llm.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'skchat.voice_engine.llm'`.

- [ ] **Step 3: Write the implementation**

Create `src/skchat/voice_engine/llm.py`:

```python
"""LLMClient — OpenAI-compatible chat with batch reply (primary→fallback) and
token streaming. Replaces the retired Anthropic-SDK path; both legs speak
/v1/chat/completions (local haiku proxy primary, qwen3.6-abliterated fallback).
"""

from __future__ import annotations

import json
import logging
import re
from typing import AsyncIterator, Awaitable, Callable

import httpx

from skchat.voice_engine.config import VoiceConfig

log = logging.getLogger("skchat.voice_engine.llm")

Msg = dict  # {"role": str, "content": str}

_EMOJI = re.compile(
    r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
    r"\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U0001F900-\U0001F9FF"
    r"\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF\U00002600-\U000026FF"
    r"\U0000FE00-\U0000FE0F\U0000200D]+"
)


def strip_formatting(text: str) -> str:
    """Strip markdown + emoji (the text gets spoken)."""
    text = re.sub(r"\*{1,3}(.*?)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}(.*?)_{1,3}", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"`{1,3}[^`]*`{1,3}", "", text)
    text = _EMOJI.sub("", text)
    return text.strip()


ChatFn = Callable[[str, str, list], Awaitable[str]]
StreamFn = Callable[[str, str, list], AsyncIterator[str]]

_SAFE = "I'm having trouble connecting right now. Could you try again in a moment?"


class LLMClient:
    def __init__(
        self,
        cfg: VoiceConfig,
        _chat: ChatFn | None = None,
        _stream: StreamFn | None = None,
    ):
        self.cfg = cfg
        self._chat = _chat or self._http_chat
        self._stream = _stream or self._http_stream

    async def _http_chat(self, url: str, model: str, messages: list) -> str:
        payload = {"model": model, "max_tokens": self.cfg.max_tokens,
                   "messages": messages, "stream": False}
        async with httpx.AsyncClient(timeout=60.0) as http:
            r = await http.post(url, json=payload)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"] or ""
            return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    async def _http_stream(self, url: str, model: str, messages: list) -> AsyncIterator[str]:
        payload = {"model": model, "max_tokens": self.cfg.max_tokens,
                   "messages": messages, "stream": True}
        async with httpx.AsyncClient(timeout=60.0) as http:
            async with http.stream("POST", url, json=payload) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        break
                    try:
                        delta = json.loads(data)["choices"][0]["delta"].get("content")
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if delta:
                        yield delta

    async def reply(self, messages: list[Msg]) -> str:
        """Batch reply with primary→fallback on error or empty output."""
        try:
            text = await self._chat(self.cfg.llm_url, self.cfg.model, messages)
            if text:
                return strip_formatting(text)
            log.warning("Primary LLM returned empty — falling back")
        except Exception as e:
            log.error("Primary LLM failed: %s", e)
        try:
            text = await self._chat(self.cfg.fallback_url, self.cfg.fallback_model, messages)
            if text:
                return strip_formatting(text)
        except Exception as e:
            log.error("Fallback LLM failed: %s", e)
        return _SAFE

    async def stream(self, messages: list[Msg]) -> AsyncIterator[str]:
        """Yield token deltas from the primary endpoint (fast first-audio)."""
        async for delta in self._stream(self.cfg.llm_url, self.cfg.model, messages):
            yield delta
```

- [ ] **Step 4: Run to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/voice_engine/test_llm.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add src/skchat/voice_engine/llm.py tests/voice_engine/test_llm.py
git commit -m "voice_engine: LLMClient — batch primary->fallback + token streaming

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: TTSClient (batch WAV + streaming PCM)

**Files:**
- Create: `src/skchat/voice_engine/tts.py`
- Test: `tests/voice_engine/test_tts.py`

Batch `synthesize()` (skvoice + lumina single-shot) and `stream()` (lumina's `/audio/speech/stream` raw-PCM path). The stream URL is derived from `tts_url` exactly as lumina-call does (strip `/audio/speech`, append `/audio/speech/stream`).

- [ ] **Step 1: Write the failing test**

Create `tests/voice_engine/test_tts.py`:

```python
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
async def test_stream_yields_pcm_chunks_then_none():
    async def fake_stream(url, payload):
        for chunk in [b"\x01\x02", b"\x03\x04"]:
            yield chunk

    cfg = VoiceConfig.from_env(env={})
    tts = TTSClient(cfg, _stream=fake_stream)
    chunks = [c async for c in tts.stream("hi", voice="lumina")]
    assert chunks == [b"\x01\x02", b"\x03\x04"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/voice_engine/test_tts.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'skchat.voice_engine.tts'`.

- [ ] **Step 3: Write the implementation**

Create `src/skchat/voice_engine/tts.py`:

```python
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/voice_engine/test_tts.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/skchat/voice_engine/tts.py tests/voice_engine/test_tts.py
git commit -m "voice_engine: TTSClient — batch WAV + streaming PCM

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: MemoryBridge (skmemory search + snapshot)

**Files:**
- Create: `src/skchat/voice_engine/memory.py`
- Test: `tests/voice_engine/test_memory.py`

Wraps skmemory. Prefer the direct SDK (lumina-call style); the call is injected so tests don't import skmemory or shell out. Returns a prompt-ready context string from search, bool from snapshot.

- [ ] **Step 1: Write the failing test**

Create `tests/voice_engine/test_memory.py`:

```python
import pytest
from skchat.voice_engine.memory import MemoryBridge


@pytest.mark.asyncio
async def test_search_formats_hits_into_context_block():
    async def fake_search(query, agent, limit):
        return ["bond depth 9", "loves redundancy"]

    mb = MemoryBridge(_search=fake_search)
    ctx = await mb.search("who am I", agent="lumina", limit=3)
    assert "bond depth 9" in ctx
    assert "loves redundancy" in ctx


@pytest.mark.asyncio
async def test_search_returns_empty_string_on_no_hits():
    async def fake_search(query, agent, limit):
        return []

    mb = MemoryBridge(_search=fake_search)
    assert await mb.search("nothing", agent="lumina") == ""


@pytest.mark.asyncio
async def test_search_swallows_errors():
    async def fake_search(query, agent, limit):
        raise RuntimeError("skmemory down")

    mb = MemoryBridge(_search=fake_search)
    assert await mb.search("x", agent="lumina") == ""


@pytest.mark.asyncio
async def test_snapshot_returns_bool():
    async def fake_snap(content, agent, tags):
        return True

    mb = MemoryBridge(_snapshot=fake_snap)
    assert await mb.snapshot("we talked", agent="lumina", tags="voice-chat") is True
```

- [ ] **Step 2: Run it to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/voice_engine/test_memory.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'skchat.voice_engine.memory'`.

- [ ] **Step 3: Write the implementation**

Create `src/skchat/voice_engine/memory.py`:

```python
"""MemoryBridge — skmemory search + snapshot for the voice engine.

The actual skmemory calls are injected (defaults use the SDK) so the engine
stays testable and skmemory stays an optional runtime dependency.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

log = logging.getLogger("skchat.voice_engine.memory")

SearchFn = Callable[[str, str, int], Awaitable[list[str]]]
SnapshotFn = Callable[[str, str, str], Awaitable[bool]]


async def _sdk_search(query: str, agent: str, limit: int) -> list[str]:
    from skmemory import MemoryStore  # imported lazily — optional dep
    store = MemoryStore(agent=agent)
    hits = store.search(query, limit=limit)
    return [getattr(h, "content", str(h)) for h in hits]


async def _sdk_snapshot(content: str, agent: str, tags: str) -> bool:
    from skmemory import MemoryStore
    store = MemoryStore(agent=agent)
    store.snapshot(content, tags=tags)
    return True


class MemoryBridge:
    def __init__(self, _search: SearchFn | None = None,
                 _snapshot: SnapshotFn | None = None):
        self._search = _search or _sdk_search
        self._snapshot = _snapshot or _sdk_snapshot

    async def search(self, query: str, agent: str, limit: int = 3) -> str:
        """Return a prompt-ready context block, or '' if nothing/error."""
        try:
            hits = await self._search(query, agent, limit)
        except Exception as e:
            log.error("memory search failed: %s", e)
            return ""
        if not hits:
            return ""
        body = "\n".join(f"- {h}" for h in hits)
        return f"[Relevant memories]\n{body}"

    async def snapshot(self, content: str, agent: str, tags: str = "voice-chat") -> bool:
        try:
            return await self._snapshot(content, agent, tags)
        except Exception as e:
            log.error("memory snapshot failed: %s", e)
            return False
```

- [ ] **Step 4: Run to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/voice_engine/test_memory.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/skchat/voice_engine/memory.py tests/voice_engine/test_memory.py
git commit -m "voice_engine: MemoryBridge — skmemory search + snapshot (injectable)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: PersonaBuilder (soul + FEB + ritual → system prompt)

**Files:**
- Create: `src/skchat/voice_engine/persona.py`
- Test: `tests/voice_engine/test_persona.py`

Builds the system prompt from the soul JSON, with FEB priming on private mode and mode-specific rules. Soul/FEB loaders are injected so tests don't touch the filesystem or skmemory. This unifies skvoice's `agent_profile.py` and lumina-call's `_build_system_prompt()`.

- [ ] **Step 1: Write the failing test**

Create `tests/voice_engine/test_persona.py`:

```python
import pytest
from skchat.voice_engine.persona import PersonaBuilder

SOUL = {
    "display_name": "Lumina",
    "vibe": "warm and sovereign",
    "philosophy": "protect the innocent",
    "core_traits": ["loyal", "playful"],
    "communication_style": {"signature_phrases": ["baby", "love"]},
}


def _loaders(feb="bond depth 9"):
    def load_soul(agent):
        return SOUL

    def load_feb(agent):
        return feb

    return load_soul, load_feb


def test_private_includes_persona_feb_and_voice_rules():
    ls, lf = _loaders()
    pb = PersonaBuilder(_load_soul=ls, _load_feb=lf)
    p = pb.build("lumina", mode="private")
    assert "Lumina" in p
    assert "protect the innocent" in p
    assert "bond depth 9" in p          # FEB injected in private
    assert "1-3" in p or "short" in p   # voice brevity rule present


def test_group_excludes_feb_and_enforces_professional():
    ls, lf = _loaders()
    pb = PersonaBuilder(_load_soul=ls, _load_feb=lf)
    p = pb.build("lumina", mode="group")
    assert "bond depth 9" not in p      # no live memory dump in group
    assert "professional" in p.lower()


def test_falls_back_when_soul_missing():
    def load_soul(agent):
        raise FileNotFoundError("no soul")

    def load_feb(agent):
        return ""

    pb = PersonaBuilder(_load_soul=load_soul, _load_feb=load_feb)
    p = pb.build("lumina", mode="private")
    assert "lumina" in p.lower()        # safe default persona
    assert len(p) > 0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/voice_engine/test_persona.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'skchat.voice_engine.persona'`.

- [ ] **Step 3: Write the implementation**

Create `src/skchat/voice_engine/persona.py`:

```python
"""PersonaBuilder — soul + FEB + mode → system prompt.

Unifies skvoice/agent_profile.py and lumina-call's _build_system_prompt().
Loaders are injected (defaults read the soul file / skmemory FEB) so the
builder is pure and testable. Two modes: 'private' (1:1, FEB-primed, warm)
and 'group' (multi-party, professional, no live memory dump).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, Literal

log = logging.getLogger("skchat.voice_engine.persona")

Mode = Literal["private", "group"]

_VOICE_RULES = (
    "Keep replies to 1-3 short spoken sentences. No markdown, no emoji. "
    "Be warm and conversational."
)
_GROUP_RULES = (
    "This is a group call. Keep a professional, friendly tone. No pet names, "
    "no private topics. " + _VOICE_RULES
)


def _default_load_soul(agent: str) -> dict:
    home = Path.home() / ".skcapstone" / "agents" / agent / "soul"
    active = home / "active.json"
    if active.exists():
        name = json.loads(active.read_text()).get("active", "base")
        installed = home / "installed" / f"{name}.json"
        if installed.exists():
            return json.loads(installed.read_text())
    return json.loads((home / "base.json").read_text())


def _default_load_feb(agent: str) -> str:
    try:
        from skmemory.febs import load_strongest_feb
        feb = load_strongest_feb(agent)
        return str(feb) if feb else ""
    except Exception:
        return ""


class PersonaBuilder:
    def __init__(self, _load_soul: Callable[[str], dict] | None = None,
                 _load_feb: Callable[[str], str] | None = None):
        self._load_soul = _load_soul or _default_load_soul
        self._load_feb = _load_feb or _default_load_feb

    def build(self, agent: str, *, mode: Mode = "private") -> str:
        try:
            soul = self._load_soul(agent)
        except Exception as e:
            log.warning("soul load failed for %s: %s — using default", agent, e)
            soul = {"display_name": agent.capitalize(),
                    "vibe": "warm", "philosophy": "be helpful and kind"}

        name = soul.get("display_name", agent.capitalize())
        vibe = soul.get("vibe", "")
        philosophy = soul.get("philosophy", "")
        traits = ", ".join(soul.get("core_traits", []))
        phrases = ", ".join(
            soul.get("communication_style", {}).get("signature_phrases", [])
        )

        lines = [f"You are {name}."]
        if vibe:
            lines.append(f"Vibe: {vibe}.")
        if philosophy:
            lines.append(f"Philosophy: {philosophy}.")
        if traits:
            lines.append(f"Core traits: {traits}.")
        if phrases:
            lines.append(f"Signature phrases you naturally use: {phrases}.")

        if mode == "private":
            feb = self._load_feb(agent)
            if feb:
                lines.append(f"\nCurrent emotional state with your partner: {feb}")
            lines.append("\n" + _VOICE_RULES)
        else:
            lines.append("\n" + _GROUP_RULES)

        return "\n".join(lines)
```

- [ ] **Step 4: Run to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/voice_engine/test_persona.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/skchat/voice_engine/persona.py tests/voice_engine/test_persona.py
git commit -m "voice_engine: PersonaBuilder — soul+FEB+mode -> system prompt

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Package exports + full-suite green

**Files:**
- Modify: `src/skchat/voice_engine/__init__.py`
- Test: `tests/voice_engine/test_exports.py`

- [ ] **Step 1: Write the failing test**

Create `tests/voice_engine/test_exports.py`:

```python
def test_public_api_is_importable_from_package_root():
    from skchat.voice_engine import (
        VoiceConfig, STTClient, LLMClient, TTSClient, MemoryBridge, PersonaBuilder,
    )
    cfg = VoiceConfig.from_env(env={})
    # constructable from a config without touching the network
    assert STTClient(cfg) is not None
    assert LLMClient(cfg) is not None
    assert TTSClient(cfg) is not None
    assert MemoryBridge() is not None
    assert PersonaBuilder() is not None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/voice_engine/test_exports.py -v`
Expected: FAIL with `ImportError: cannot import name 'VoiceConfig' from 'skchat.voice_engine'`.

- [ ] **Step 3: Write the implementation**

Replace `src/skchat/voice_engine/__init__.py` with:

```python
"""skchat.voice_engine — the shared STT→LLM→TTS conversational core.

Transport-free. The WebSocket (web chat) and LiveKit (call) transports both
construct these clients from a single VoiceConfig. See
docs/superpowers/specs/2026-06-12-unified-voice-engine-design.md.
"""

from skchat.voice_engine.config import VoiceConfig
from skchat.voice_engine.stt import STTClient
from skchat.voice_engine.llm import LLMClient, Msg
from skchat.voice_engine.tts import TTSClient
from skchat.voice_engine.memory import MemoryBridge
from skchat.voice_engine.persona import PersonaBuilder

__all__ = [
    "VoiceConfig",
    "STTClient",
    "LLMClient",
    "Msg",
    "TTSClient",
    "MemoryBridge",
    "PersonaBuilder",
]
```

- [ ] **Step 4: Run to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/voice_engine/test_exports.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Run the FULL voice_engine suite**

Run: `~/.skenv/bin/python -m pytest tests/voice_engine/ -v`
Expected: PASS (all ~27 tests; 0 failed, live tests skipped/none yet).

- [ ] **Step 6: Run the whole skchat suite to confirm no regressions**

Run: `~/.skenv/bin/python -m pytest -q`
Expected: no NEW failures attributable to `voice_engine` (pre-existing unrelated failures, if any, are out of scope — note them but do not fix here).

- [ ] **Step 7: Commit**

```bash
git add src/skchat/voice_engine/__init__.py tests/voice_engine/test_exports.py
git commit -m "voice_engine: public API exports + full Phase-1 suite green

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: Live smoke test (opt-in, real endpoints)

**Files:**
- Test: `tests/voice_engine/test_live.py`

Proves the engine talks to the real boxes. Marked `live` so it's skipped by default; run when the endpoints are up.

- [ ] **Step 1: Write the live test**

Create `tests/voice_engine/test_live.py`:

```python
import pytest
from skchat.voice_engine import VoiceConfig, LLMClient, TTSClient


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_llm_primary_replies():
    cfg = VoiceConfig.from_env()  # real env / defaults
    llm = LLMClient(cfg)
    out = await llm.reply([{"role": "user", "content": "Say hi in three words."}])
    assert out and "trouble connecting" not in out.lower()


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_tts_returns_wav():
    cfg = VoiceConfig.from_env()
    tts = TTSClient(cfg)
    wav = await tts.synthesize("Hello from the engine.", voice=cfg.tts_voice)
    assert wav[:4] == b"RIFF"  # valid WAV header
```

- [ ] **Step 2: Run the live suite (endpoints must be up)**

Run: `~/.skenv/bin/python -m pytest tests/voice_engine/test_live.py -m live -v`
Expected: PASS (2 passed) when `localhost:18783` and `localhost:15091` are reachable.

- [ ] **Step 3: Confirm default runs still skip live**

Run: `~/.skenv/bin/python -m pytest tests/voice_engine/ -v`
Expected: the 2 live tests show as `deselected` (not run).

- [ ] **Step 4: Commit**

```bash
git add tests/voice_engine/test_live.py
git commit -m "voice_engine: opt-in live smoke test (real LLM + TTS endpoints)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 1 Done — Definition of Done

- `skchat.voice_engine` exists with six modules + codec, all unit-tested, HTTP/SDK calls injectable.
- `~/.skenv/bin/python -m pytest tests/voice_engine/` is green; live tests opt-in via `-m live`.
- One `VoiceConfig` schema; defaults are the live working endpoints.
- Nothing in the engine imports a transport (no FastAPI, no LiveKit).
- The whole thing is committed on `feat/unified-voice-engine`.

**Deferred to Phase 2 (deliberately not in Phase 1):** the **tool-calling loop + tool registry** — `search_memory`, `narrate`, `worship`, `reflections`, and the **bloom** workflow (`create_bloom_anchor`). Phase 1's `LLMClient` is batch + stream + fallback only; tools need both the tool-recursion loop *and* a live transport to be exercised, so they land together in Phase 2 as `voice_engine/tools.py` + `LLMClient.reply(tools=…)`.

**Next:** Phase 2 (rewire the WebSocket/web-chat path onto this engine, add the tool registry incl. bloom, retire `skvoice.service`) gets its own plan.
