def test_package_imports():
    import skchat.voice_engine  # noqa: F401


def test_voice_engine_constructs_without_network():
    """VoiceEngine can be instantiated from a blank config with no live endpoints."""
    from skchat.voice_engine import VoiceConfig, VoiceEngine

    cfg = VoiceConfig.from_env(env={})
    eng = VoiceEngine(cfg, "lumina")
    assert eng.agent == "lumina"
    assert eng.llm is not None
    assert eng.memory is not None
    assert eng.persona is not None


def test_transports_package_importable():
    import skchat.transports  # noqa: F401
    from skchat.transports.websocket import build_app  # noqa: F401

    app = build_app(engine_factory=lambda agent: None)
    assert app is not None
