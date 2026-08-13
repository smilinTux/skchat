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
            seen["messages"] = messages
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
    assert seen["force_tool"] == "narrate"  # narrate intent forced in sacred

    # Memory is injected, but as its OWN system message, never glued onto the
    # user's turn. This assertion used to require the opposite and so encoded a
    # live bug: with the memories sitting above his sentence in the same user
    # message, she answered the MEMORIES. On a real call 2026-08-13 "testing
    # one, two, three" got a reply about the Al-Asad withdrawal.
    assert seen["user"] == "tell me a story"  # the user turn is ONLY what he said
    mem_msgs = [
        m for m in seen["messages"] if m["role"] == "system" and "bond depth 9" in m["content"]
    ]
    assert len(mem_msgs) == 1, "memory must ride in exactly one system message"
    assert "not what the user just said" in mem_msgs[0]["content"].lower()
    # ...and immediately before the user turn, so the framing is read last.
    assert seen["messages"].index(mem_msgs[0]) == len(seen["messages"]) - 2
