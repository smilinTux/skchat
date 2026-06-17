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


def test_phase2_api_is_importable_from_package_root():
    from skchat.voice_engine import (
        Tool,
        ToolRegistry,
        VoiceConfig,
        VoiceEngine,
        build_default_registry,
        wants_action,
        wants_narrate,
    )

    cfg = VoiceConfig.from_env(env={})
    # VoiceEngine constructable without network
    eng = VoiceEngine(cfg, "lumina")
    assert eng is not None
    assert eng.agent == "lumina"

    # ToolRegistry + Tool
    reg = ToolRegistry()
    reg.register(
        Tool(name="test_tool", schema={"type": "function", "function": {"name": "test_tool"}})
    )
    assert reg.openai_schemas()[0]["function"]["name"] == "test_tool"

    # Intent detectors
    assert wants_narrate("tell me a story")
    assert wants_action("check my email")
    assert not wants_narrate("what is 2+2")
    assert not wants_action("how are you")

    # build_default_registry smoke
    default_reg = build_default_registry(cfg, "lumina")
    names = {s["function"]["name"] for s in default_reg.openai_schemas()}
    assert "search_memory" in names
    assert "narrate" in names
