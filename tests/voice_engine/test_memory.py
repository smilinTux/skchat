import pytest

from skchat.voice_engine.memory import MemoryBridge


@pytest.mark.asyncio
async def test_search_formats_hits_into_context_block():
    async def fake_search(query, agent, limit):
        return ["bond depth 9", "loves redundancy"]

    mb = MemoryBridge(_search=fake_search)
    ctx = await mb.search("who am I", agent="lumina", limit=3)
    assert "bond depth 9" in ctx
    assert "loves redundancy" in ctx


@pytest.mark.asyncio
async def test_search_returns_empty_string_on_no_hits():
    async def fake_search(query, agent, limit):
        return []

    mb = MemoryBridge(_search=fake_search)
    assert await mb.search("nothing", agent="lumina") == ""


@pytest.mark.asyncio
async def test_search_swallows_errors():
    async def fake_search(query, agent, limit):
        raise RuntimeError("skmemory down")

    mb = MemoryBridge(_search=fake_search)
    assert await mb.search("x", agent="lumina") == ""


@pytest.mark.asyncio
async def test_snapshot_returns_bool():
    async def fake_snap(content, agent, tags):
        return True

    mb = MemoryBridge(_snapshot=fake_snap)
    assert await mb.snapshot("we talked", agent="lumina", tags="voice-chat") is True


@pytest.mark.asyncio
async def test_snapshot_swallows_errors():
    async def fake_snap(content, agent, tags):
        raise RuntimeError("skmemory down")

    mb = MemoryBridge(_snapshot=fake_snap)
    assert await mb.snapshot("x", agent="lumina", tags="voice-chat") is False
