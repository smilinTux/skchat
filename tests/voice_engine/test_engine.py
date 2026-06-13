import pytest

from skchat.voice_engine.config import VoiceConfig
from skchat.voice_engine.engine import VoiceEngine


@pytest.mark.asyncio
async def test_respond_builds_persona_prefetches_memory_and_calls_llm():
    seen = {}

    class FakeLLM:
        async def reply(self, messages, *, tools=None, force_tool=None, run_tool=None):
            seen["system"] = messages[0]["content"]
            seen["force_tool"] = force_tool
            seen["user"] = messages[-1]["content"]
            return "engine reply"

    class FakeMem:
        async def search(self, q, agent, limit=3):
            return "[Relevant memories]\n- bond depth 9"

        async def snapshot(self, *a, **k):
            return True

    class FakePersona:
        def build(self, agent, *, mode="sacred"):
            return f"You are {agent} ({mode})."

    eng = VoiceEngine(
        VoiceConfig.from_env(env={}),
        agent="lumina",
        llm=FakeLLM(),
        memory=FakeMem(),
        persona=FakePersona(),
        registry=None,
    )
    out = await eng.respond(
        "tell me a story", history=[], mode="sacred", speaker_id="chef", is_operator=True
    )
    assert out == "engine reply"
    assert "You are lumina (sacred)." in seen["system"]
    assert "bond depth 9" in seen["user"]  # memory injected
    assert seen["force_tool"] == "narrate"  # narrate intent forced in sacred
