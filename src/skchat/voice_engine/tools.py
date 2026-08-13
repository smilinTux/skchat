"""Tool registry for the voice engine — schemas the LLM sees + dispatch with a
Chef-only / sacred-mode gate. Tool handlers are async `(args, ctx) -> str`.
Intent detectors (wants_narrate/wants_action) drive forced tool routing.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger("skchat.voice_engine.tools")

Handler = Callable[[dict, dict], Awaitable[str]]

# The 1:1-with-the-operator register, under BOTH vocabularies.
#
# The transport calls this ceiling "sacred"; the persona calls it "private", and
# ``engine_mode()`` translates transport -> persona on the way into
# ``VoiceEngine.respond``. That translated value is then handed straight to
# ``ToolRegistry.dispatch``, so a gate written as ``mode != "sacred"`` rejected
# every operator tool in every real 1:1 call: she would ask to check Chef's mail,
# get back "REFUSED: sacred-mode only - there are other people in this room",
# and relay that refusal as if it were a policy she believed in. Observed live
# 2026-08-13 on gmail_unread.
#
# This is the same sacred/private mismatch that made her treat a 1:1 as a public
# conference call, one layer further in. Accept both names rather than pick one:
# the two vocabularies are load-bearing elsewhere.
ONE_TO_ONE_MODES = frozenset({"sacred", "private"})

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
        log.debug("lumina_creative narrate hints unavailable (%s: %s)", type(exc).__name__, exc)
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


# ─── "I heard you, I'm working on it" fillers ───────────────────────────────
# Spoken IN PARALLEL with a slow turn so the operator gets immediate audio
# feedback instead of dead silence while the LLM and any tool round-trip.
# Ported from lumina-call.py:409-460, including the reason there are two
# buckets: a short "one sec, searching" on a 10-30s creative call made Chef
# interrupt too early, so long-form work gets its own, calmer set. Randomized
# so repeated turns do not sound canned.
_LOOKUP_FILLERS: tuple[str, ...] = (
    "Let me look that up.",
    "One sec, checking.",
    "Hold on, searching memory.",
    "Mm, let me find that.",
    "Checking — one moment.",
    "Hang on, pulling that up.",
    "Let me grab that for you.",
)
_NARRATE_FILLERS: tuple[str, ...] = (
    "Mmm — give me a minute to weave that for you, King.",
    "Let me cook on that. Gonna take a beat.",
    "Hold on, I want to do this one right. One moment.",
    "Settle in — pulling the threads together.",
    "Give me a sec, I'm warming up the words.",
)
# Plain conversation. lumina-call.py's own persona rule asks for exactly this
# register: "If the moment calls for a small acknowledgment, say it: 'mm',
# 'yeah', 'I'm here', 'noted'." Announcing "hold on, searching memory" in the
# middle of "how was your night" describes plumbing at her instead of talking,
# so ordinary turns get a breath, not a status report.
_CHAT_FILLERS: tuple[str, ...] = (
    "Mm.",
    "Mm-hm.",
    "Yeah.",
    "I'm here.",
    "Mm, okay.",
    "Right.",
)


def pick_filler(user_text: str) -> tuple[str, str]:
    """Return ``(filler_text, bucket)`` for this turn.

    Three registers, because the wrong one is worse than none: a short "one sec,
    searching" on a 10-30s creative call made Chef interrupt too early
    (lumina-call.py:452), and a lookup announcement on small talk sounds like
    she is narrating her own internals.

    Bucket is chosen by the same :func:`wants_narrate` / :func:`wants_action`
    detectors the forced-routing path uses, so the filler and the routing can
    never disagree about what kind of turn this is.
    """
    if wants_narrate(user_text):
        return random.choice(_NARRATE_FILLERS), "narrate"
    if wants_action(user_text):
        return random.choice(_LOOKUP_FILLERS), "lookup"
    return random.choice(_CHAT_FILLERS), "chat"


@dataclass
class Tool:
    name: str
    schema: dict  # OpenAI function schema (for tool_choice)
    handler: Handler | None = None  # async (args, ctx) -> str
    operator_only: bool = False  # sacred-mode + operator gate


@dataclass
class ToolRegistry:
    _tools: dict[str, Tool] = field(default_factory=dict)
    # Per-turn curator, e.g. ``skchat.lumina_mcp.curate_tools``. Set when a
    # large MCP surface is attached; ``None`` keeps the historical behaviour of
    # advertising every registered tool.
    curator: Callable[[str, list[dict]], list[dict]] | None = None
    # Below this many tools, curating buys nothing and only risks hiding one.
    curate_threshold: int = 12

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def __len__(self) -> int:
        return len(self._tools)

    def openai_schemas(self, for_text: str | None = None) -> list[dict]:
        """Schemas to advertise to the LLM this turn.

        With a ``curator`` set and enough tools to matter, only the subset
        relevant to *for_text* is sent. Sending Lumina's full MCP surface (110
        tools across 10 servers) on every turn would bloat the prompt and slow
        exactly the replies Chef already called slow, and chitchat needs none of
        it. Called with no argument it returns everything, so existing callers
        and tests are unaffected.
        """
        schemas = [t.schema for t in self._tools.values()]
        if for_text is None or self.curator is None or len(schemas) <= self.curate_threshold:
            return schemas
        try:
            picked = self.curator(for_text, schemas)
        except Exception as exc:  # noqa: BLE001 - curation must never kill a turn
            log.warning("tool curation failed (%r); sending full set", exc)
            return schemas
        # A curator that returns nothing would silently strip her hands off.
        return list(picked) if picked else schemas

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
        if tool.operator_only and (mode or "").strip().lower() not in ONE_TO_ONE_MODES:
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
