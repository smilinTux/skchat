"""Tests for VoiceSession — the per-connection state holder + convo factory.

Pure state object; no transport, no engine, no network. The convo factory is
verified to produce immutable Conversation snapshots wired for
``VoiceEngine.respond(conversation=...)``.
"""

import pytest

from skchat.voice_engine.conversation import Conversation
from skchat.voice_engine.voice_session import VoiceSession


def test_defaults():
    vs = VoiceSession()
    assert vs.session_id.startswith("vs_")
    assert vs.history == []
    assert vs.mode == "sacred"
    assert vs.speaker_id == ""
    assert vs.is_operator is True
    assert len(vs) == 0


def test_explicit_session_id_preserved():
    vs = VoiceSession(session_id="lumina:42")
    assert vs.session_id == "lumina:42"


def test_conversation_factory_snapshots_state():
    vs = VoiceSession(session_id="s1", mode="group", speaker_id="chef", is_operator=False)
    vs.history.append({"role": "user", "content": "earlier"})

    convo = vs.conversation("hello there", timestamp=99.0)
    assert isinstance(convo, Conversation)
    assert convo.transcript == "hello there"
    assert convo.session_id == "s1"
    assert convo.mode == "group"
    assert convo.speaker_id == "chef"
    assert convo.is_operator is False
    assert convo.timestamp == 99.0
    assert convo.history == ({"role": "user", "content": "earlier"},)
    assert isinstance(convo.history, tuple)  # immutable snapshot


def test_conversation_snapshot_is_decoupled_from_session():
    """Mutating the session after minting must not affect a prior snapshot."""
    vs = VoiceSession(session_id="s1")
    vs.history.append({"role": "user", "content": "a"})
    convo = vs.conversation("turn")
    vs.history.append({"role": "assistant", "content": "b"})
    # snapshot froze at one entry
    assert convo.history == ({"role": "user", "content": "a"},)


def test_conversation_default_timestamp_is_set():
    vs = VoiceSession()
    convo = vs.conversation("hi")
    assert convo.timestamp > 0


def test_add_turn_appends_user_and_assistant():
    vs = VoiceSession()
    vs.add_turn("hi", "hello")
    assert vs.history == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert len(vs) == 2


def test_add_turn_skips_empty_parts():
    vs = VoiceSession()
    vs.add_turn("", "only-assistant")
    assert vs.history == [{"role": "assistant", "content": "only-assistant"}]
    vs.add_turn("only-user", "")
    assert vs.history[-1] == {"role": "user", "content": "only-user"}


def test_history_trimmed_past_cap():
    vs = VoiceSession(history_cap=4)
    for i in range(10):
        vs.add_turn(f"u{i}", f"a{i}")
    # capped: trimmed to the most recent 30 entries (but never grew past
    # cap by more than one exchange before trimming)
    assert len(vs) <= 30
    # most recent exchange survives
    assert vs.history[-1] == {"role": "assistant", "content": "a9"}


def test_clear_drops_history():
    vs = VoiceSession()
    vs.add_turn("hi", "yo")
    vs.clear()
    assert vs.history == []
    assert len(vs) == 0


def test_set_speaker_updates_fields():
    vs = VoiceSession()
    vs.set_speaker("chef", is_operator=True)
    assert vs.speaker_id == "chef"
    assert vs.is_operator is True
    vs.set_speaker("guest", is_operator=False)
    assert vs.speaker_id == "guest"
    assert vs.is_operator is False


def test_set_speaker_without_operator_flag_leaves_it():
    vs = VoiceSession(is_operator=True)
    vs.set_speaker("chef")
    assert vs.speaker_id == "chef"
    assert vs.is_operator is True  # unchanged


def test_set_mode():
    vs = VoiceSession()
    vs.set_mode("private")
    assert vs.mode == "private"
    # next snapshot reflects the new mode
    assert vs.conversation("x").mode == "private"


@pytest.mark.asyncio
async def test_round_trips_through_voice_engine_respond():
    """The convo factory output flows through respond() into ctx['convo']."""
    from skchat.voice_engine.config import VoiceConfig
    from skchat.voice_engine.engine import VoiceEngine
    from skchat.voice_engine.tools import Tool, ToolRegistry

    seen = {}

    async def tool_fn(args, ctx):
        seen["convo"] = ctx.get("convo")
        return "ok"

    class FakeLLM:
        async def reply(self, messages, *, tools=None, force_tool=None, run_tool=None):
            return await run_tool("probe", {})

    class FakeMem:
        async def search(self, q, agent, limit=3):
            return ""

    class FakePersona:
        def build(self, agent, *, mode="sacred"):
            return "You are lumina."

    reg = ToolRegistry()
    reg.register(
        Tool(
            name="probe",
            schema={"type": "function", "function": {"name": "probe"}},
            handler=tool_fn,
            operator_only=False,
        )
    )

    vs = VoiceSession(session_id="ses-1", speaker_id="chef", is_operator=True)
    convo = vs.conversation("tell me a story")
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
        history=list(convo.history),
        mode=convo.mode,
        speaker_id=convo.speaker_id,
        is_operator=convo.is_operator,
        conversation=convo,
    )
    assert out == "ok"
    assert seen["convo"] is convo
    assert seen["convo"].session_id == "ses-1"
