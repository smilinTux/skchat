"""skchat.voice_engine — the shared STT→LLM→TTS conversational core.

Transport-free. The WebSocket (web chat) and LiveKit (call) transports both
construct these clients from a single VoiceConfig. See
docs/superpowers/specs/2026-06-12-unified-voice-engine-design.md.
"""
