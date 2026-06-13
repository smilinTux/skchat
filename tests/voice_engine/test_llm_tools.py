import pytest

from skchat.voice_engine.config import VoiceConfig
from skchat.voice_engine.llm import LLMClient


def _cfg():
    return VoiceConfig.from_env(env={})


@pytest.mark.asyncio
async def test_reply_runs_tool_then_final_text():
    # round 0 returns a tool_call; round 1 returns final text
    rounds = [
        {
            "tool_calls": [
                {
                    "id": "1",
                    "function": {"name": "search_memory", "arguments": '{"query":"x"}'},
                }
            ],
            "content": "",
        },
        {"tool_calls": [], "content": "Here is what I found."},
    ]

    async def fake_raw(url, model, messages, *, tool_choice=None):
        return rounds.pop(0)

    async def run_tool(name, args):
        return "MEMORY: bond depth 9"

    llm = LLMClient(_cfg(), _chat_raw=fake_raw)
    out = await llm.reply(
        [{"role": "user", "content": "who am i"}],
        tools=[{"type": "function", "function": {"name": "search_memory"}}],
        run_tool=run_tool,
    )
    assert out == "Here is what I found."


@pytest.mark.asyncio
async def test_force_tool_sets_tool_choice_round0():
    seen = {}

    async def fake_raw(url, model, messages, *, tool_choice=None):
        seen.setdefault("choices", []).append(tool_choice)
        return {"tool_calls": [], "content": "ok"}

    llm = LLMClient(_cfg(), _chat_raw=fake_raw)
    await llm.reply(
        [{"role": "user", "content": "x"}],
        tools=[{"type": "function", "function": {"name": "narrate"}}],
        force_tool="narrate",
        run_tool=lambda n, a: _async("s"),
    )
    assert seen["choices"][0] == {"type": "function", "function": {"name": "narrate"}}


@pytest.mark.asyncio
async def test_narrate_result_returned_verbatim():
    rounds = [
        {
            "tool_calls": [{"id": "1", "function": {"name": "narrate", "arguments": "{}"}}],
            "content": "",
        }
    ]

    async def fake_raw(url, model, messages, *, tool_choice=None):
        return rounds.pop(0)

    async def run_tool(name, args):
        return "The air in the kitchen is thick, heavy with the scent of " * 4

    llm = LLMClient(_cfg(), _chat_raw=fake_raw)
    out = await llm.reply(
        [{"role": "user", "content": "story"}],
        tools=[{"type": "function", "function": {"name": "narrate"}}],
        force_tool="narrate",
        run_tool=run_tool,
    )
    assert out.startswith("The air in the kitchen")  # verbatim, not summarized


async def _async(v):
    return v
