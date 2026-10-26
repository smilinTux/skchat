"""Persona + memory context are built ONCE per session, never per turn.

Chef's rule (2026-10-26): profile / persona / memory context is injected at
session start, for every agent runtime. These pin it for VoiceEngine, which
both the LiveKit and WebSocket transports drive.
"""

from datetime import datetime

import pytest

from skchat.voice_engine import memory_context
from skchat.voice_engine.config import VoiceConfig
from skchat.voice_engine.engine import VoiceEngine


class _LLM:
    def __init__(self):
        self.calls = []

    async def reply(self, messages, *, tools=None, force_tool=None, run_tool=None):
        self.calls.append(messages)
        return "ok"


class _Mem:
    def __init__(self):
        self.searches = 0

    async def search(self, q, agent, limit=3):
        self.searches += 1
        return "[Relevant memories]\n- should never appear"

    async def snapshot(self, *a, **k):
        return True


class _Persona:
    def __init__(self):
        self.builds = []

    def build(self, agent, *, mode="private"):
        self.builds.append(mode)
        return f"You are {agent} ({mode})."


def _engine(env=None, loader=None):
    llm, mem, persona = _LLM(), _Mem(), _Persona()
    loads = []

    def default_loader(agent, budget):
        loads.append((agent, budget))
        return memory_context.HEADER + "\n\nSubconscious digest: Chef shipped skcall PR 16.", {}

    eng = VoiceEngine(
        VoiceConfig.from_env(env=env or {}),
        agent="lumina",
        llm=llm,
        memory=mem,
        persona=persona,
        registry=None,
        context_loader=loader or default_loader,
    )
    return eng, llm, mem, persona, loads


async def _turns(eng, texts, mode="sacred"):
    history = []
    for t in texts:
        reply = await eng.respond(t, history, mode=mode)
        history += [{"role": "user", "content": t}, {"role": "assistant", "content": reply}]


@pytest.mark.asyncio
async def test_persona_and_memory_load_once_across_turns():
    eng, llm, mem, persona, loads = _engine()
    await _turns(eng, ["hi", "how are you", "what did we do today"])
    assert persona.builds == ["sacred"]
    assert len(loads) == 1
    assert eng.persona_builds == 1 and eng.context_loads == 1


@pytest.mark.asyncio
async def test_no_per_turn_memory_search_by_default():
    eng, llm, mem, persona, loads = _engine()
    await _turns(eng, ["hi", "remember the beach?"])
    assert mem.searches == 0 and eng.memory_searches == 0
    for messages in llm.calls:
        assert not any("should never appear" in m["content"] for m in messages)
        assert sum(1 for m in messages if m["role"] == "system") == 1


@pytest.mark.asyncio
async def test_system_prompt_stable_across_turns_and_carries_memory_block():
    eng, llm, *_ = _engine()
    await _turns(eng, ["one", "two", "three"])
    systems = [m[0]["content"] for m in llm.calls]
    assert len(set(systems)) == 1
    assert "Chef shipped skcall PR 16" in systems[0]
    # Brevity stays last; memory sits above it.
    assert systems[0].rstrip().endswith("Warm and conversational.")
    assert systems[0].index("Chef shipped") < systems[0].index("LENGTH LIMIT")


@pytest.mark.asyncio
async def test_group_register_gets_no_memory_block_unless_opted_in():
    eng, llm, *_ = _engine()
    await _turns(eng, ["hello all"], mode="group")
    assert "Chef shipped" not in llm.calls[0][0]["content"]

    eng, llm, *_ = _engine(env={"SKVOICE_CONTEXT_GROUP": "1"})
    await _turns(eng, ["hello all"], mode="group")
    assert "Chef shipped" in llm.calls[0][0]["content"]


@pytest.mark.asyncio
async def test_mode_switch_builds_each_register_once_and_reloads_nothing():
    eng, llm, mem, persona, loads = _engine(env={"SKVOICE_CONTEXT_GROUP": "1"})
    for mode in ("sacred", "group", "sacred", "group"):
        await eng.respond("x", [], mode=mode)
    assert persona.builds == ["sacred", "group"]
    assert len(loads) == 1


@pytest.mark.asyncio
async def test_context_flag_off_skips_loader():
    eng, llm, mem, persona, loads = _engine(env={"SKVOICE_CONTEXT": "0"})
    await _turns(eng, ["hi", "again"])
    assert loads == []
    assert "What you remember" not in llm.calls[0][0]["content"]


@pytest.mark.asyncio
async def test_budget_env_reaches_loader():
    eng, llm, mem, persona, loads = _engine(env={"SKVOICE_CONTEXT_BUDGET": "1234"})
    await _turns(eng, ["hi"])
    assert loads == [("lumina", 1234)]


@pytest.mark.asyncio
async def test_refresh_and_end_session_invalidate_the_cache():
    eng, llm, mem, persona, loads = _engine()
    await _turns(eng, ["a", "b"])
    eng.refresh_context()
    await _turns(eng, ["c"])
    assert persona.builds == ["sacred", "sacred"] and len(loads) == 2
    eng.end_session()
    await _turns(eng, ["d"])
    assert len(persona.builds) == 3 and len(loads) == 3


@pytest.mark.asyncio
async def test_loader_failure_never_fails_the_turn():
    def boom(agent, budget):
        raise RuntimeError("disk gone")

    eng, llm, *_ = _engine(loader=boom)
    await _turns(eng, ["hi", "still there?"])
    assert len(llm.calls) == 2
    assert eng.context_loads == 1  # tried once, not retried every turn


def test_build_call_context_reads_whisper_reflection_and_journal(tmp_path):
    home = tmp_path / "lumina"
    (home / "skwhisper").mkdir(parents=True)
    (home / "skwhisper" / "whisper.md").write_text(
        "# SKWhisper Context\n> Auto-generated\n\n## Recent\n- Chef asked about skcall\n"
    )
    (home / "reflections").mkdir()
    (home / "reflections" / "2026-10-25_reflection.md").write_text("A good day of building.")
    (home / "journal.md").write_text(
        "## Daemon shutdown\nnoise\n\n## Shipped the voice context fix\nIt landed.\n"
    )
    block, report = memory_context.build_call_context(
        "lumina", 3000, home=home, now=datetime(2026, 10, 26)
    )
    assert block.startswith(memory_context.HEADER)
    assert "Chef asked about skcall" in block
    assert "SKWhisper Context" not in block
    assert "A good day of building." in block
    assert "Shipped the voice context fix" in block
    assert "Daemon shutdown" not in block
    assert report["whisper"] and report["reflection"] and report["journal"]


def test_build_call_context_skips_stale_reflection_and_respects_zero_budget(tmp_path):
    home = tmp_path / "lumina"
    (home / "reflections").mkdir(parents=True)
    (home / "reflections" / "2026-09-01_reflection.md").write_text("old news")
    block, _ = memory_context.build_call_context(
        "lumina", 3000, home=home, now=datetime(2026, 10, 26)
    )
    assert block == ""
    assert memory_context.build_call_context("lumina", 0, home=home)[0] == ""


def test_config_flags_default():
    cfg = VoiceConfig.from_env(env={})
    assert cfg.context_enabled is True
    assert cfg.context_budget == 3000
    assert cfg.context_group is False
    assert cfg.per_turn_recall is False
