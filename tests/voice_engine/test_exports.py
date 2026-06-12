def test_public_api_is_importable_from_package_root():
    from skchat.voice_engine import (
        LLMClient,
        MemoryBridge,
        PersonaBuilder,
        STTClient,
        TTSClient,
        VoiceConfig,
    )
    cfg = VoiceConfig.from_env(env={})
    # constructable from a config without touching the network
    assert STTClient(cfg) is not None
    assert LLMClient(cfg) is not None
    assert TTSClient(cfg) is not None
    assert MemoryBridge() is not None
    assert PersonaBuilder() is not None
