"""Forced routing must fire under BOTH names for the 1:1 register.

The transport calls it "sacred", the persona calls it "private", and
engine_mode() translates between them on the way into VoiceEngine.respond. A
check written against one name silently does nothing on a real call. This has
now bitten three times: the group-call persona branch, the operator-tool gate,
and narrate forcing.
"""

import pytest

from skchat.voice_engine.config import VoiceConfig
from skchat.voice_engine.engine import VoiceEngine
from skchat.voice_engine.tools import ONE_TO_ONE_MODES


class _FakeLLM:
    def __init__(self):
        self.force_tool = "UNSET"

    async def reply(self, messages, *, tools=None, force_tool=None, run_tool=None):
        self.force_tool = force_tool
        return "ok"


class _FakeMem:
    async def search(self, q, agent, limit=3):
        return ""

    async def snapshot(self, *a, **k):
        return True


class _FakePersona:
    def build(self, agent, *, mode="sacred"):
        return f"You are {agent} ({mode})."


def _engine(llm):
    return VoiceEngine(
        VoiceConfig.from_env(env={}),
        agent="lumina",
        llm=llm,
        memory=_FakeMem(),
        persona=_FakePersona(),
        registry=None,
    )


@pytest.mark.parametrize("mode", sorted(ONE_TO_ONE_MODES))
@pytest.mark.asyncio
async def test_narrate_is_forced_in_both_one_to_one_vocabularies(mode):
    llm = _FakeLLM()
    await _engine(llm).respond("narrate something for me", history=[], mode=mode)
    assert llm.force_tool == "narrate", f"narrate not forced in mode={mode!r}"


@pytest.mark.asyncio
async def test_narrate_is_not_forced_in_a_group_room():
    """The privacy gate: never force intimate narration with others present."""
    llm = _FakeLLM()
    await _engine(llm).respond("narrate something for me", history=[], mode="group")
    assert llm.force_tool != "narrate"
