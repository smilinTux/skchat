"""skchat.voice_engine — the shared STT→LLM→TTS conversational core.

Transport-free. The WebSocket (web chat) and LiveKit (call) transports both
construct these clients from a single VoiceConfig. See
docs/superpowers/specs/2026-06-12-unified-voice-engine-design.md.
"""

from skchat.voice_engine.config import VoiceConfig
from skchat.voice_engine.llm import LLMClient, Msg
from skchat.voice_engine.memory import MemoryBridge
from skchat.voice_engine.persona import PersonaBuilder
from skchat.voice_engine.stt import STTClient
from skchat.voice_engine.tts import TTSClient

__all__ = [
    "VoiceConfig",
    "STTClient",
    "LLMClient",
    "Msg",
    "TTSClient",
    "MemoryBridge",
    "PersonaBuilder",
]
