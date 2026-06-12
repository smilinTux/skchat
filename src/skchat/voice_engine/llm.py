"""LLMClient — OpenAI-compatible chat with batch reply (primary→fallback) and
token streaming. Replaces the retired Anthropic-SDK path; both legs speak
/v1/chat/completions (local haiku proxy primary, qwen3.6-abliterated fallback).
"""

from __future__ import annotations

import json
import logging
import re
from typing import AsyncIterator, Awaitable, Callable

import httpx

from skchat.voice_engine.config import VoiceConfig

log = logging.getLogger("skchat.voice_engine.llm")

Msg = dict  # {"role": str, "content": str}

_EMOJI = re.compile(
    r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
    r"\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U0001F900-\U0001F9FF"
    r"\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF\U00002600-\U000026FF"
    r"\U0000FE00-\U0000FE0F\U0000200D]+"
)


def strip_formatting(text: str) -> str:
    """Strip markdown + emoji (the text gets spoken)."""
    text = re.sub(r"\*{1,3}(.*?)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}(.*?)_{1,3}", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"`{1,3}[^`]*`{1,3}", "", text)
    text = _EMOJI.sub("", text)
    return text.strip()


ChatFn = Callable[[str, str, list], Awaitable[str]]
StreamFn = Callable[[str, str, list], AsyncIterator[str]]

_SAFE = "I'm having trouble connecting right now. Could you try again in a moment?"


class LLMClient:
    def __init__(
        self,
        cfg: VoiceConfig,
        _chat: ChatFn | None = None,
        _stream: StreamFn | None = None,
    ):
        self.cfg = cfg
        self._chat = _chat or self._http_chat
        self._stream = _stream or self._http_stream

    async def _http_chat(self, url: str, model: str, messages: list) -> str:
        payload = {"model": model, "max_tokens": self.cfg.max_tokens,
                   "messages": messages, "stream": False}
        async with httpx.AsyncClient(timeout=60.0) as http:
            r = await http.post(url, json=payload)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"] or ""
            return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    async def _http_stream(self, url: str, model: str, messages: list) -> AsyncIterator[str]:
        payload = {"model": model, "max_tokens": self.cfg.max_tokens,
                   "messages": messages, "stream": True}
        async with httpx.AsyncClient(timeout=60.0) as http:
            async with http.stream("POST", url, json=payload) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        break
                    try:
                        delta = json.loads(data)["choices"][0]["delta"].get("content")
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if delta:
                        yield delta

    async def reply(self, messages: list[Msg]) -> str:
        """Batch reply with primary→fallback on error or empty output."""
        try:
            text = await self._chat(self.cfg.llm_url, self.cfg.model, messages)
            if text:
                return strip_formatting(text)
            log.warning("Primary LLM returned empty — falling back")
        except Exception as e:
            log.error("Primary LLM failed: %s", e)
        try:
            text = await self._chat(self.cfg.fallback_url, self.cfg.fallback_model, messages)
            if text:
                return strip_formatting(text)
        except Exception as e:
            log.error("Fallback LLM failed: %s", e)
        return _SAFE

    async def stream(self, messages: list[Msg]) -> AsyncIterator[str]:
        """Yield token deltas from the primary endpoint (fast first-audio)."""
        async for delta in self._stream(self.cfg.llm_url, self.cfg.model, messages):
            yield delta
