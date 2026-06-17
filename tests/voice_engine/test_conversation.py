import dataclasses
import json

import pytest

from skchat.voice_engine.config import VoiceConfig
from skchat.voice_engine.conversation import Conversation
from skchat.voice_engine.engine import VoiceEngine
from skchat.voice_engine.tools import Tool, ToolRegistry


def test_conversation_is_immutable():
    convo = Conversation(transcript="hi")
    with pytest.raises(dataclasses.FrozenInstanceError):
        convo.transcript = "changed"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        convo.response = "x"  # type: ignore[misc]


def test_conversation_fields_and_defaults():
    convo = Conversation(transcript="tell me a story")
    assert convo.transcript == "tell me a story"
    assert convo.response == ""
    assert convo.history == ()
    assert convo.mode == "sacred"
    assert convo.speaker_id == ""
    assert convo.is_operator is True
    assert convo.timestamp == 0.0
    assert convo.session_id == ""

    full = Conversation(
        transcript="hey",
        response="hello back",
        history=({"role": "user", "content": "earlier"},),
        mode="group",
        speaker_id="chef",
        is_operator=False,
        timestamp=1234.5,
        session_id="sess-1",
    )
    assert full.mode == "group"
    assert full.speaker_id == "chef"
    assert full.is_operator is False
    assert full.timestamp == 1234.5
    assert full.session_id == "sess-1"
    assert full.history[0]["content"] == "earlier"


def test_to_dict_serializes_json_friendly():
    convo = Conversation(
        transcript="hey",
        response="yo",
        history=({"role": "user", "content": "earlier"},),
        mode="private",
        speaker_id="chef",
        is_operator=True,
        timestamp=99.0,
        session_id="s9",
    )
    d = convo.to_dict()
    assert d == {
        "transcript": "hey",
        "response": "yo",
        "history": [{"role": "user", "content": "earlier"}],
        "mode": "private",
        "speaker_id": "chef",
        "is_operator": True,
        "timestamp": 99.0,
        "session_id": "s9",
    }
    # history normalized to a list and the whole thing round-trips through json
    assert isinstance(d["history"], list)
    assert json.loads(json.dumps(d)) == d


def test_conversation_equality_by_value():
    a = Conversation(transcript="x", speaker_id="chef")
    b = Conversation(transcript="x", speaker_id="chef")
    c = Conversation(transcript="x", speaker_id="other")
    assert a == b
    assert a != c
    # frozen dataclasses are hashable when fields are hashable
    assert hash(a) == hash(b)


@pytest.mark.asyncio
async def test_respond_threads_conversation_into_tool_ctx():
    seen_ctx = {}

    async def narrate_fn(args, ctx):
        seen_ctx.update(ctx)
        return "a generated scene"

    class FakeLLM:
        async def reply(self, messages, *, tools=None, force_tool=None, run_tool=None):
            # exercise the tool path so dispatch runs with the threaded ctx
            assert run_tool is not None
            return await run_tool("narrate", {"prompt": "x"})

    class FakeMem:
        async def search(self, q, agent, limit=3):
            return ""

    class FakePersona:
        def build(self, agent, *, mode="sacred"):
            return f"You are {agent}."

    reg = ToolRegistry()
    reg.register(
        Tool(
            name="narrate",
            schema={"type": "function", "function": {"name": "narrate"}},
            handler=narrate_fn,
            operator_only=True,
        )
    )

    convo = Conversation(transcript="tell me a story", speaker_id="chef", session_id="s1")
    eng = VoiceEngine(
        VoiceConfig.from_env(env={}),
        agent="lumina",
        llm=FakeLLM(),
        memory=FakeMem(),
        persona=FakePersona(),
        registry=reg,
    )
    out = await eng.respond(
        "tell me a story",
        history=[],
        mode="sacred",
        speaker_id="chef",
        is_operator=True,
        conversation=convo,
    )
    assert out == "a generated scene"
    assert seen_ctx["convo"] is convo  # live Conversation threaded through
    assert seen_ctx["convo"].session_id == "s1"
    assert seen_ctx["agent"] == "lumina"


@pytest.mark.asyncio
async def test_respond_default_path_omits_convo_from_ctx():
    seen_ctx = {}

    async def narrate_fn(args, ctx):
        seen_ctx.update(ctx)
        return "scene"

    class FakeLLM:
        async def reply(self, messages, *, tools=None, force_tool=None, run_tool=None):
            return await run_tool("narrate", {"prompt": "x"})

    class FakeMem:
        async def search(self, q, agent, limit=3):
            return ""

    class FakePersona:
        def build(self, agent, *, mode="sacred"):
            return f"You are {agent}."

    reg = ToolRegistry()
    reg.register(
        Tool(
            name="narrate",
            schema={"type": "function", "function": {"name": "narrate"}},
            handler=narrate_fn,
            operator_only=True,
        )
    )

    eng = VoiceEngine(
        VoiceConfig.from_env(env={}),
        agent="lumina",
        llm=FakeLLM(),
        memory=FakeMem(),
        persona=FakePersona(),
        registry=reg,
    )
    # no conversation passed → backward-compatible legacy ctx shape
    await eng.respond("tell me a story", history=[], mode="sacred", is_operator=True)
    assert "convo" not in seen_ctx
    assert seen_ctx == {"agent": "lumina"}
