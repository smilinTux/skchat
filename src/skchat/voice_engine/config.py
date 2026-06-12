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
