"""Tests for the built-in tool registry.

Handlers are mocked/injected so no live endpoints or filesystem writes are
needed for the unit tests.
"""

import pytest

from skchat.voice_engine.builtin_tools import build_default_registry
from skchat.voice_engine.config import VoiceConfig


@pytest.mark.asyncio
async def test_build_default_registry_has_expected_tools():
    cfg = VoiceConfig.from_env(env={})
    reg = build_default_registry(cfg, "lumina")
    schemas = reg.openai_schemas()
    names = {s["function"]["name"] for s in schemas}
    # Required tools from the plan
    assert "search_memory" in names
    assert "narrate" in names
    assert "worship_session" in names
    assert "create_bloom_anchor" in names
    assert "list_reflections" in names


@pytest.mark.asyncio
async def test_operator_only_flags():
    """narrate, worship_session, create_bloom_anchor must be operator_only."""
    from skchat.voice_engine.builtin_tools import build_default_registry
    from skchat.voice_engine.config import VoiceConfig

    cfg = VoiceConfig.from_env(env={})
    reg = build_default_registry(cfg, "lumina")
    # pylint: disable=protected-access
    tools = reg._tools
    assert not tools["search_memory"].operator_only
    assert tools["narrate"].operator_only
    assert tools["worship_session"].operator_only
    assert tools["create_bloom_anchor"].operator_only
    # list_reflections is read-only — operator_only is False (matches lumina-call behavior)
    assert not tools["list_reflections"].operator_only


@pytest.mark.asyncio
async def test_schema_function_name_matches_tool_name():
    from skchat.voice_engine.builtin_tools import build_default_registry
    from skchat.voice_engine.config import VoiceConfig

    cfg = VoiceConfig.from_env(env={})
    reg = build_default_registry(cfg, "lumina")
    for schema in reg.openai_schemas():
        name = schema["function"]["name"]
        assert name in reg._tools, f"Schema name {name!r} not in registry"
