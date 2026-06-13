"""Live integration tests for the voice_engine default loaders.

These tests use the REAL skmemory SDK and real agent files — no injected fakes.
They are skipped in the default suite and must be opted-in with -m live.

Run:
    cd ~ && ~/.skenv/bin/python -m pytest /path/to/tests/voice_engine/ -m live -v
"""

import pytest

from skchat.voice_engine import MemoryBridge, PersonaBuilder, VoiceConfig, VoiceEngine


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_memory_snapshot_then_search(monkeypatch):
    """Snapshot a marker memory then search for it — exercises _sdk_snapshot + _sdk_search."""
    monkeypatch.setenv("SKAGENT", "lumina")
    monkeypatch.setenv("SKCAPSTONE_AGENT", "lumina")
    mb = MemoryBridge()  # REAL skmemory defaults
    ok = await mb.snapshot(
        "voice engine live test marker alpha",
        agent="lumina",
        tags="voice-chat-test",
    )
    assert ok is True

    ctx = await mb.search("voice engine live test marker", agent="lumina", limit=3)
    # search returns a context block string or "" — must not raise
    assert isinstance(ctx, str)


@pytest.mark.live
def test_live_persona_loads_active_soul(monkeypatch):
    """Build a persona using the real soul file and real FEB dir."""
    monkeypatch.setenv("SKAGENT", "lumina")
    monkeypatch.setenv("SKCAPSTONE_AGENT", "lumina")
    pb = PersonaBuilder()  # REAL soul + FEB loaders
    p = pb.build("lumina", mode="private")
    assert "Lumina" in p and len(p) > 0  # real soul loaded, no crash

    g = pb.build("lumina", mode="group")
    assert "professional" in g.lower()


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_voice_engine_respond(monkeypatch):
    """VoiceEngine.respond with real LLM endpoint returns a non-empty string."""
    monkeypatch.setenv("SKAGENT", "lumina")
    monkeypatch.setenv("SKCAPSTONE_AGENT", "lumina")
    cfg = VoiceConfig.from_env()
    eng = VoiceEngine(cfg, "lumina")
    reply = await eng.respond(
        "say hi in three words", [], mode="sacred", speaker_id="chef", is_operator=True
    )
    assert reply and "trouble connecting" not in reply.lower()
    assert len(reply) > 0
