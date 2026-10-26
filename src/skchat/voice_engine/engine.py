"""VoiceEngine — one-turn orchestrator that wires persona + memory + LLM + tools.

Transports (WebSocket, LiveKit) own the session/turn loop; VoiceEngine owns the
brain. Each call to `respond()` is a single conversational turn.

One engine is one session: both transports build a VoiceEngine per LiveKit
call / WebSocket connection. Persona (soul + FEB), the runtime fact, the
agent's memory block (memory_context.py) and the brevity rule are built ONCE
per session per register and cached, so every turn sends the same system
prompt (Chef's rule, 2026-10-26: context is injected at session start, never
per turn). Mid-call recall is on demand through the search_memory tool. The
old per-turn skmemory search is behind SKVOICE_PER_TURN_RECALL=1 (default off).
The cache is dropped only by `refresh_context()` / `end_session()`.

Usage:
    eng = VoiceEngine(cfg, "lumina")
    reply = await eng.respond(transcript, history, mode="sacred", speaker_id="chef")
"""

from __future__ import annotations

import logging
from typing import Callable, Literal

from skchat.voice_engine import memory_context
from skchat.voice_engine.config import VoiceConfig
from skchat.voice_engine.conversation import Conversation
from skchat.voice_engine.llm import LLMClient
from skchat.voice_engine.memory import MemoryBridge
from skchat.voice_engine.persona import PersonaBuilder
from skchat.voice_engine.tools import (
    ONE_TO_ONE_MODES,
    ToolRegistry,
    wants_action,
    wants_narrate,
)

log = logging.getLogger("skchat.voice_engine.engine")

Mode = Literal["sacred", "group", "private"]

_BREVITY_RULE = (
    "LENGTH LIMIT, this overrides any other instruction about how much to say: "
    "reply in AT MOST 2 short spoken sentences, then stop. This is a phone call, "
    "not an essay: say the one thing that matters and let the other person "
    "answer. If there is more to say, say the first part and wait to be asked. "
    "Never list, never enumerate, never summarize your own reasoning. "
    "No markdown, no emoji, no stage directions. Warm and conversational."
)


class VoiceEngine:
    """Orchestrates one conversational turn: persona → memory → LLM + tools.

    All dependencies are injected so the engine is fully testable without
    live endpoints.  Defaults construct the Phase-1 clients from `cfg`.
    """

    def __init__(
        self,
        cfg: VoiceConfig,
        agent: str = "lumina",
        *,
        stt=None,  # STTClient — not used by the brain; transport calls STT
        llm: LLMClient | None = None,
        tts=None,  # TTSClient — not used by the brain; transport calls TTS
        memory: MemoryBridge | None = None,
        persona: PersonaBuilder | None = None,
        registry: ToolRegistry | None = None,
        context_loader: Callable[[str, int], tuple[str, dict]] | None = None,
    ):
        self.cfg = cfg
        self.agent = agent
        self.stt = stt
        self.llm = llm if llm is not None else LLMClient(cfg)
        self.tts = tts
        self.memory = memory if memory is not None else MemoryBridge()
        self.persona = persona if persona is not None else PersonaBuilder()
        self.registry = registry
        # Session-scoped context, built on first use and then reused verbatim.
        self._context_loader = context_loader or memory_context.build_call_context
        self._system_cache: dict[str, str] = {}
        self._memory_context: str | None = None
        self.context_report: dict = {}
        # Counters so a synthetic turn can prove the rule (tests, deploy check).
        self.persona_builds = 0
        self.context_loads = 0
        self.memory_searches = 0

    # -- session context (built once) ---------------------------------------

    def _session_memory(self) -> str:
        """The agent's memory block, loaded once per session ('' if off)."""
        if self._memory_context is None:
            self._memory_context = ""
            if getattr(self.cfg, "context_enabled", True):
                budget = getattr(self.cfg, "context_budget", memory_context.DEFAULT_BUDGET_TOKENS)
                try:
                    text, report = self._context_loader(self.agent, budget)
                except Exception:  # noqa: BLE001 - never fail a session over context
                    log.warning("memory context unavailable; persona only", exc_info=True)
                    text, report = "", {"error": True}
                self.context_loads += 1
                self._memory_context = text or ""
                self.context_report = report or {}
                log.info("memory context for %s: %s", self.agent, self.context_report)
            else:
                self.context_report = {"enabled": False}
        return self._memory_context

    def system_prompt(self, mode: str) -> str:
        """The fixed system prompt for this session in *mode*, built once."""
        cached = self._system_cache.get(mode)
        if cached is not None:
            return cached
        self.persona_builds += 1
        text = self._build_system_prompt(mode)
        self._system_cache[mode] = text
        return text

    def refresh_context(self) -> None:
        """Explicit refresh: the next turn rebuilds persona + memory block."""
        self._system_cache.clear()
        self._memory_context = None
        self.context_report = {}

    def end_session(self) -> None:
        """Session over: drop the cached context."""
        self.refresh_context()

    def _build_system_prompt(self, mode: str) -> str:
        """Persona + runtime fact + memory block + brevity. Called once per mode."""
        system_text = self.persona.build(self.agent, mode=mode)
        # Tell her what she is actually running on. A model cannot introspect its
        # own weights, so asked "what model are you using right now" she invents a
        # plausible name: on 2026-08-13 she answered "Claude 3.5 Sonnet" while
        # served by claude-haiku-4-5. Chef reads that as hallucination, and he is
        # right. State the fact instead of leaving her to guess.
        #
        # But state ONLY the model name. The first version handed her the
        # endpoint and the fallback model too, and she read the lot back on a
        # call ("running on claude-haiku-4-5 at localhost, fallback to
        # qwen3.6-27b-abliterated if needed"), which is infrastructure spilling
        # into a conversation. It also broadcast a stale config value as if it
        # were true: that fallback model is not loaded anywhere.
        system_text += (
            f"\n\nRuntime fact: the model you run on is '{self.cfg.model}'. State that "
            f"name if you are asked what model you are, and never guess a different "
            f"one. Do not recite it otherwise, and never read out endpoints, URLs, "
            f"ports or fallback configuration: those are infrastructure, not "
            f"conversation."
        )
        # The agent's own memory, loaded once per session. Private register
        # only unless SKVOICE_CONTEXT_GROUP=1: whisper.md is private material.
        if mode in ONE_TO_ONE_MODES or getattr(self.cfg, "context_group", False):
            mem_block = self._session_memory()
            if mem_block:
                system_text += "\n\n" + mem_block
        # Brevity goes LAST, always. The persona is long and expansive and the
        # rule was being buried in the middle of it: measured live on
        # 2026-08-13, replies ran 22-38 SECONDS of speech while this asked for
        # 1-3 sentences. At F5's ~0.33x realtime that is 7-10s of synthesis
        # before she says a word, which is most of what "she's slow" was.
        if _BREVITY_RULE not in system_text:
            system_text = system_text + "\n\n" + _BREVITY_RULE
        return system_text

    async def respond(
        self,
        transcript: str,
        history: list[dict],
        *,
        mode: str = "sacred",
        speaker_id: str = "",
        is_operator: bool = True,
        conversation: Conversation | None = None,
    ) -> str:
        """Run one turn: persona + memory + forced-routing + LLM + tools.

        Args:
            transcript: The user's spoken/typed text for this turn.
            history:    Conversation history (list of {role, content} dicts).
                        VoiceEngine does NOT mutate this; the transport manages
                        history append/cap after receiving the reply.
            mode:       'sacred' (1-on-1 with operator), 'group', or 'private'.
            speaker_id: Identity of the speaker (used by the operator gate).
            is_operator: True when the speaker is the operator (Chef).
            conversation: Optional immutable Conversation snapshot for this
                        turn. When provided it is threaded into the tool ctx as
                        ``ctx['convo']`` so tool handlers can read live
                        conversation context. Backward-compatible default (None)
                        leaves the legacy ctx shape unchanged.

        Returns:
            The LLM's reply as a plain string ready for TTS.
        """
        # 1. The session's fixed system prompt (persona + runtime fact + memory
        #    block + brevity rule), built on the first turn and reused verbatim.
        system_msg = {"role": "system", "content": self.system_prompt(mode)}

        # 2. Fetch relevant memories. These go in their OWN system message, NOT
        #    glued onto the front of what the user said.
        #
        #    Concatenating them into the user turn made the model answer the
        #    MEMORIES instead of the person. Observed live 2026-08-13 on a call:
        #    Chef said "testing one, two, three" and got a reply about the
        #    Al-Asad withdrawal; he asked "what model are you using right now"
        #    and got a reply about conversations where she had claimed SSH
        #    access. Both were recalled memories sitting above his sentence in
        #    the same message, and it read as hallucination.
        #
        #    Off by default since 2026-10-26 (Chef's rule: memory is injected
        #    once at session start, never per turn). The session memory block
        #    sits in the system prompt, and she can call search_memory when a
        #    turn needs more. SKVOICE_PER_TURN_RECALL=1 restores the old path.
        mem = ""
        if getattr(self.cfg, "per_turn_recall", False):
            self.memory_searches += 1
            mem = await self.memory.search(transcript, self.agent)
        memory_msg = (
            {
                "role": "system",
                "content": (
                    "Background memories retrieved for context only. They are NOT "
                    "what the user just said and may be stale or irrelevant. Do not "
                    "respond to them or bring them up unless they answer the user's "
                    "actual message.\n\n" + mem
                ),
            }
            if mem
            else None
        )
        user_msg = {"role": "user", "content": transcript}

        # 3. Forced-routing decision (mirrors lumina-call.py Conversation loop).
        #    narrate forced only 1:1 with the operator, to respect the group
        #    privacy gate.
        #
        #    This read `mode == "sacred"` and so was NEVER true on a real call:
        #    the transport calls the 1:1 register "sacred", the persona calls it
        #    "private", and engine_mode() translates on the way in here. Asked
        #    "tell me a worship story" on 2026-08-13 she routed it as a narrate
        #    turn (the filler picked the narrate bucket) and then, with nothing
        #    forcing the tool, simply chatted about it: "I'm not quite
        #    following, are you saying you want a beach scene?" The capability
        #    was attached and curated in; only the forcing was missing.
        #
        #    Third place this exact sacred/private mismatch has bitten, after
        #    the group-call persona branch and the operator-tool gate. Both
        #    names are load-bearing, so accept both rather than picking one.
        if wants_narrate(transcript) and mode in ONE_TO_ONE_MODES:
            force_tool: str | None = "narrate"
        elif wants_action(transcript):
            force_tool = "required"
        else:
            force_tool = None

        # 4. Prepare tools from the registry (if any).
        # Curate against THIS turn's transcript, not the whole surface. With an
        # MCP registry attached that is the difference between advertising ~110
        # tools and the handful this sentence could possibly need.
        tools = self.registry.openai_schemas(for_text=transcript) if self.registry else None

        tool_ctx: dict = {"agent": self.agent}
        if conversation is not None:
            tool_ctx["convo"] = conversation

        def _run_tool(name: str, args: dict):
            return self.registry.dispatch(
                name,
                args,
                speaker_id=speaker_id,
                mode=mode,
                is_operator=is_operator,
                ctx=tool_ctx,
            )

        run_tool = _run_tool if self.registry else None

        # 5. Build the full message list and call the LLM.
        messages = [system_msg, *history, user_msg]
        if memory_msg is not None:
            # Directly before the user turn, so the "this is background" framing
            # is the last thing read before the sentence it must not answer.
            messages.insert(-1, memory_msg)
        return await self.llm.reply(
            messages,
            tools=tools,
            force_tool=force_tool,
            run_tool=run_tool,
        )
