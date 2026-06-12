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
