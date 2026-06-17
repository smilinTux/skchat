"""Tool registry for the voice engine — schemas the LLM sees + dispatch with a
Chef-only / sacred-mode gate. Tool handlers are async `(args, ctx) -> str`.
Intent detectors (wants_narrate/wants_action) drive forced tool routing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger("skchat.voice_engine.tools")

Handler = Callable[[dict, dict], Awaitable[str]]

# Neutral base only. The intimate/escalation trigger vocabulary lives in the
# private lumina_creative package and is merged in when installed — keeping sacred
# trigger words out of this public repo (same pattern as lumina_mcp's tool merge).
# Without lumina_creative, only these generic narration triggers apply.
_NARRATE_HINTS_BASE: tuple[str, ...] = (
    "narrate",
    "narrative",
)


def _load_narrate_hints() -> tuple[str, ...]:
    hints = _NARRATE_HINTS_BASE
    try:
        from lumina_creative.routing import NARRATE_HINTS as _extra

        hints = hints + tuple(_extra)
    except Exception as exc:  # private package absent — neutral base only
        log.debug("lumina_creative narrate hints unavailable (%s: %s)",
                  type(exc).__name__, exc)
    return hints


_NARRATE_HINTS = _load_narrate_hints()
_ACTION_HINTS = (
    "email",
    "emails",
    "inbox",
    "gmail",
    "unread",
    "my calendar",
    "my schedule",
    "schedule",
    "agenda",
    "appointment",
    "what's on my",
    "whats on my",
    "what do i have",
    "remind me",
    "set a reminder",
    "send a message to",
    "send a text",
    "google drive",
    "my contacts",
)


def wants_narrate(text: str) -> bool:
    t = (text or "").lower()
    return any(h in t for h in _NARRATE_HINTS)


def wants_action(text: str) -> bool:
    t = (text or "").lower()
    return any(h in t for h in _ACTION_HINTS)


@dataclass
class Tool:
    name: str
    schema: dict  # OpenAI function schema (for tool_choice)
    handler: Handler | None = None  # async (args, ctx) -> str
    operator_only: bool = False  # sacred-mode + operator gate


@dataclass
class ToolRegistry:
    _tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def openai_schemas(self) -> list[dict]:
        return [t.schema for t in self._tools.values()]

    async def dispatch(
        self,
        name: str,
        args: dict,
        *,
        speaker_id: str = "",
        mode: str = "sacred",
        is_operator: bool = True,
        ctx: dict | None = None,
    ) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"unknown tool: {name}"
        # Chef-only gate: powerful/operator tools require the operator AND
        # (for operator_only ones) sacred mode.
        if not is_operator:
            return f"PERMISSION DENIED: '{name}' can only be run when the operator asks."
        if tool.operator_only and mode != "sacred":
            return f"REFUSED: '{name}' is sacred-mode only — there are other people in this room."
        if tool.handler is None:
            return f"tool {name} has no handler"
        # Thread the full ctx (including ctx['convo'], the live Conversation
        # snapshot when supplied by the engine) through to the handler so tools
        # can read live conversation context.
        handler_ctx = ctx or {}
        try:
            return await tool.handler(args, handler_ctx)
        except Exception as exc:  # noqa: BLE001
            log.warning("tool %s failed: %r", name, exc)
            return f"{name} failed: {exc}"
