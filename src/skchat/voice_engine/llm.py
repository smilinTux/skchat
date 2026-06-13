"""LLMClient — OpenAI-compatible chat with batch reply (primary→fallback),
token streaming, and tool-calling with forced routing + narrate-verbatim.

Both legs speak /v1/chat/completions (local haiku proxy primary,
qwen3.6-abliterated fallback).

Tool-calling API:
    reply(messages, tools=..., force_tool=..., run_tool=...)

When `tools` is None the method behaves exactly as Phase 1
(primary→fallback, strip_formatting, safe message on error).
When `tools` is provided the method runs the tool-recursion loop (up to 4
rounds); `force_tool` sets tool_choice on round 0; a successful `narrate`
result is returned verbatim.
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


def strip_think(text: str) -> str:
    """Remove qwen-style <think> reasoning. Handles closed tags AND tags
    left unclosed by max_tokens truncation (strip from <think> to end)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<think>.*$", "", text, flags=re.DOTALL)
    return text.strip()


ChatFn = Callable[[str, str, list], Awaitable[str]]
StreamFn = Callable[[str, str, list], AsyncIterator[str]]
# Low-level raw chat fn used in the tool-calling loop.
# Signature: (url, model, messages, *, tool_choice=None) -> dict
# Returns {"content": str, "tool_calls": list}
RawChatFn = Callable[..., Awaitable[dict]]

_SAFE = "I'm having trouble connecting right now. Could you try again in a moment?"


class LLMClient:
    def __init__(
        self,
        cfg: VoiceConfig,
        _chat: ChatFn | None = None,
        _stream: StreamFn | None = None,
        _chat_raw: RawChatFn | None = None,
    ):
        self.cfg = cfg
        self._chat = _chat or self._http_chat
        self._stream = _stream or self._http_stream
        self._chat_raw = _chat_raw or self._http_chat_raw

    async def _http_chat(self, url: str, model: str, messages: list) -> str:
        payload = {
            "model": model,
            "max_tokens": self.cfg.max_tokens,
            "messages": messages,
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=60.0) as http:
            r = await http.post(url, json=payload)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"] or ""
            return strip_think(text)

    async def _http_chat_raw(
        self,
        url: str,
        model: str,
        messages: list,
        *,
        tool_choice=None,
    ) -> dict:
        """Low-level POST to /v1/chat/completions; returns {"content","tool_calls"}.

        The `_active_tools` attribute is set by `_reply_with_tools` so the HTTP
        implementation can include the tools list without it being part of the
        injected-fake interface (test fakes only accept tool_choice).
        """
        payload: dict = {
            "model": model,
            "max_tokens": self.cfg.max_tokens,
            "messages": messages,
            "stream": False,
            "temperature": 0.7,
        }
        active_tools = getattr(self, "_active_tools", None)
        if active_tools:
            payload["tools"] = active_tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        async with httpx.AsyncClient(timeout=90.0) as http:
            r = await http.post(url, json=payload)
            r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        return {
            "content": (msg.get("content") or "").strip(),
            "tool_calls": msg.get("tool_calls") or [],
        }

    async def _http_stream(self, url: str, model: str, messages: list) -> AsyncIterator[str]:
        payload = {
            "model": model,
            "max_tokens": self.cfg.max_tokens,
            "messages": messages,
            "stream": True,
        }
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

    async def reply(
        self,
        messages: list[Msg],
        *,
        tools: list | None = None,
        force_tool: str | None = None,
        run_tool: Callable[[str, dict], Awaitable[str]] | None = None,
    ) -> str:
        """Batch reply.

        - When `tools` is None: primary→fallback with strip_formatting (Phase-1
          behavior, safe message on total failure).
        - When `tools` is provided: tool-recursion loop (up to 4 rounds).
          `force_tool` sets tool_choice on round 0. A successful narrate result
          is returned VERBATIM without a summarize round.
        """
        if tools is None:
            return await self._reply_plain(messages)
        return await self._reply_with_tools(
            messages, tools=tools, force_tool=force_tool, run_tool=run_tool
        )

    async def _reply_plain(self, messages: list[Msg]) -> str:
        """Phase-1 primary→fallback batch reply."""
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

    async def _reply_with_tools(
        self,
        messages: list[Msg],
        *,
        tools: list,
        force_tool: str | None,
        run_tool: Callable[[str, dict], Awaitable[str]] | None,
    ) -> str:
        """Tool-recursion loop ported from lumina-call.py `llm_reply()`.

        Round 0: if force_tool, set tool_choice.
        On tool_calls: run each via run_tool; if a 'narrate' result is long and
        non-error, return it VERBATIM (no summarize round).
        Otherwise loop up to 4 rounds total.
        """
        # Build working message list (passed by value so we don't mutate caller's)
        msgs = list(messages)
        text = ""

        for tool_round in range(4):
            tool_choice = None
            if force_tool and tool_round == 0:
                tool_choice = (
                    "required"
                    if force_tool == "required"
                    else {"type": "function", "function": {"name": force_tool}}
                )

            # _active_tools is scoped to the single call so _http_chat_raw can
            # include the tool schemas without changing the injected-fake
            # interface; cleared in finally so it never leaks past the call.
            self._active_tools = tools
            try:
                result = await self._chat_raw(
                    self.cfg.llm_url,
                    self.cfg.model,
                    msgs,
                    tool_choice=tool_choice,
                )
            except Exception as exc:
                log.error("LLM (tool round %d) failed: %s", tool_round, exc)
                break
            finally:
                self._active_tools = None

            text = result.get("content") or ""
            tool_calls = result.get("tool_calls") or []

            if tool_calls:
                # Append assistant's tool-call message, run each tool, then loop.
                msgs.append({
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": tool_calls,
                })
                narrate_result = None
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    name = fn.get("name") or ""
                    raw_args = fn.get("arguments") or "{}"
                    try:
                        args = (
                            json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                        )
                    except Exception:
                        args = {}
                    log.info("tool %s(%s)", name, json.dumps(args)[:80])
                    if run_tool is not None:
                        try:
                            result_str = await run_tool(name, args)
                        except Exception as exc:
                            result_str = f"{name} error: {exc}"
                    else:
                        result_str = f"tool {name} has no runner"
                    log.info("tool %s → %s", name, result_str[:80].replace("\n", " "))
                    # narrate verbatim guard: a long successful narrate result is spoken
                    # as-is — routing it back through the orchestrator LLM produces a
                    # sanitized summary instead of the actual prose.
                    if (
                        name == "narrate"
                        and len(result_str) > 80
                        and not result_str.lower().startswith("narrate")
                    ):
                        narrate_result = result_str
                    msgs.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id") or "",
                        "content": result_str,
                    })
                if narrate_result is not None:
                    return narrate_result
                continue  # next round: model reads tool results + produces final reply

            # No tool_calls — final reply
            text = strip_think(text)
            return strip_formatting(text) if text else _SAFE

        # Hit recursion cap
        if text:
            return strip_formatting(strip_think(text))
        return "I tried to look something up but lost my thread — say it again?"

    async def stream(self, messages: list[Msg]) -> AsyncIterator[str]:
        """Yield token deltas from the primary endpoint (fast first-audio).

        NOTE: deltas are RAW — no <think> stripping or markdown filtering is
        applied here. The streaming transport (Phase 2) is responsible for
        sentence assembly and filtering before audio is handed to TTS. The
        primary endpoint (claude-haiku proxy) does not emit <think> tags, so
        this is a documented boundary rather than a runtime concern.
        """
        async for delta in self._stream(self.cfg.llm_url, self.cfg.model, messages):
            yield delta
