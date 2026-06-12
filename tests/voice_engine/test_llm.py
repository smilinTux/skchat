import pytest

from skchat.voice_engine.config import VoiceConfig
from skchat.voice_engine.llm import LLMClient, strip_formatting, strip_think


def test_strip_formatting_removes_markdown_and_emoji():
    assert strip_formatting("**hi** _there_") == "hi there"
    assert strip_formatting("Hello 😊 world").strip() == "Hello  world".strip()


@pytest.mark.asyncio
async def test_reply_uses_primary_when_it_succeeds():
    seen = []

    async def fake_chat(url, model, messages):
        seen.append((url, model))
        return "primary says hi"

    cfg = VoiceConfig.from_env(env={})
    llm = LLMClient(cfg, _chat=fake_chat)
    out = await llm.reply([{"role": "user", "content": "hi"}])
    assert out == "primary says hi"
    assert seen[0] == (cfg.llm_url, cfg.model)


@pytest.mark.asyncio
async def test_reply_falls_back_on_primary_error():
    calls = []

    async def fake_chat(url, model, messages):
        calls.append(url)
        if url == cfg.llm_url:
            raise RuntimeError("429 rate limit")
        return "fallback says hi"

    cfg = VoiceConfig.from_env(env={})
    llm = LLMClient(cfg, _chat=fake_chat)
    out = await llm.reply([{"role": "user", "content": "hi"}])
    assert out == "fallback says hi"
    assert calls == [cfg.llm_url, cfg.fallback_url]


@pytest.mark.asyncio
async def test_reply_falls_back_on_empty_primary():
    async def fake_chat(url, model, messages):
        return "" if url == cfg.llm_url else "fallback text"

    cfg = VoiceConfig.from_env(env={})
    llm = LLMClient(cfg, _chat=fake_chat)
    out = await llm.reply([{"role": "user", "content": "hi"}])
    assert out == "fallback text"


@pytest.mark.asyncio
async def test_reply_returns_safe_message_when_both_fail():
    async def fake_chat(url, model, messages):
        raise RuntimeError("down")

    cfg = VoiceConfig.from_env(env={})
    llm = LLMClient(cfg, _chat=fake_chat)
    out = await llm.reply([{"role": "user", "content": "hi"}])
    assert "trouble connecting" in out.lower()


@pytest.mark.asyncio
async def test_stream_yields_deltas():
    async def fake_stream(url, model, messages):
        for tok in ["Hel", "lo ", "there"]:
            yield tok

    cfg = VoiceConfig.from_env(env={})
    llm = LLMClient(cfg, _stream=fake_stream)
    got = [t async for t in llm.stream([{"role": "user", "content": "hi"}])]
    assert "".join(got) == "Hello there"


def test_strip_think_removes_closed_block():
    assert strip_think("<think>hmm let me see</think>Hello there") == "Hello there"


def test_strip_think_removes_truncated_unclosed_block():
    # qwen truncated mid-think at max_tokens — no closing tag
    assert strip_think("Sure!\n<think>I should consider whether") == "Sure!"


def test_strip_think_noop_when_absent():
    assert strip_think("Just a normal reply.") == "Just a normal reply."
