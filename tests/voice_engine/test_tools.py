import pytest

from skchat.voice_engine.tools import Tool, ToolRegistry, wants_action, wants_narrate


def test_wants_narrate_and_action_detectors():
    assert wants_narrate("tell me a story") and wants_narrate("make it more explicit")
    assert not wants_narrate("what time is it")
    assert wants_action("check my email") and wants_action("what's on my calendar")
    assert not wants_action("how are you")


@pytest.mark.asyncio
async def test_registry_dispatch_and_operator_gate():
    calls = []

    async def narrate_fn(args, ctx):
        calls.append(args)
        return "a long generated scene " * 5

    reg = ToolRegistry()
    reg.register(
        Tool(
            name="narrate",
            schema={"type": "function", "function": {"name": "narrate"}},
            handler=narrate_fn,
            operator_only=True,
        )
    )
    # non-operator in group mode is refused, handler not called
    out = await reg.dispatch(
        "narrate", {"prompt": "x"}, speaker_id="stranger", mode="group", is_operator=False
    )
    assert "REFUSED" in out or "only" in out.lower()
    assert calls == []
    # operator in sacred mode runs it
    out = await reg.dispatch(
        "narrate", {"prompt": "x"}, speaker_id="chef", mode="sacred", is_operator=True
    )
    assert "generated scene" in out
    assert calls


def test_openai_schemas_for_llm():
    reg = ToolRegistry()
    reg.register(
        Tool(
            name="search_memory",
            schema={"type": "function", "function": {"name": "search_memory"}},
            handler=None,
        )
    )
    schemas = reg.openai_schemas()
    assert schemas and schemas[0]["function"]["name"] == "search_memory"
