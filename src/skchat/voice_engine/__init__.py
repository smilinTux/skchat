"""skchat.voice_engine — the shared STT→LLM→TTS conversational core.

Transport-free. The WebSocket (web chat) and LiveKit (call) transports both
construct these clients from a single VoiceConfig. See
docs/superpowers/specs/2026-06-12-unified-voice-engine-design.md.

Phase 2 additions: VoiceEngine orchestrator, ToolRegistry, Tool, intent
detectors, and build_default_registry.
"""

from skchat.voice_engine.builtin_tools import build_default_registry
from skchat.voice_engine.config import VoiceConfig
from skchat.voice_engine.conversation import Conversation
from skchat.voice_engine.engine import VoiceEngine
from skchat.voice_engine.llm import LLMClient, Msg
from skchat.voice_engine.memory import MemoryBridge
from skchat.voice_engine.persona import PersonaBuilder
from skchat.voice_engine.stt import STTClient
from skchat.voice_engine.tools import Tool, ToolRegistry, wants_action, wants_narrate
from skchat.voice_engine.tts import TTSClient

__all__ = [
    "VoiceConfig",
    "STTClient",
    "LLMClient",
    "Msg",
    "TTSClient",
    "MemoryBridge",
    "PersonaBuilder",
    "VoiceEngine",
    "Conversation",
    "ToolRegistry",
    "Tool",
    "wants_narrate",
    "wants_action",
    "build_default_registry",
]
