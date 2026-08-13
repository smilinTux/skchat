"""VoiceEngine — one-turn orchestrator that wires persona + memory + LLM + tools.

Transports (WebSocket, LiveKit) own the session/turn loop; VoiceEngine owns the
brain. Each call to `respond()` is a single conversational turn.

Usage:
    eng = VoiceEngine(cfg, "lumina")
    reply = await eng.respond(transcript, history, mode="sacred", speaker_id="chef")
"""

from __future__ import annotations

import logging
from typing import Literal

from skchat.voice_engine.config import VoiceConfig
from skchat.voice_engine.conversation import Conversation
from skchat.voice_engine.llm import LLMClient
from skchat.voice_engine.memory import MemoryBridge
from skchat.voice_engine.persona import PersonaBuilder
from skchat.voice_engine.tools import ToolRegistry, wants_action, wants_narrate

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
    ):
        self.cfg = cfg
        self.agent = agent
        self.stt = stt
        self.llm = llm if llm is not None else LLMClient(cfg)
        self.tts = tts
        self.memory = memory if memory is not None else MemoryBridge()
        self.persona = persona if persona is not None else PersonaBuilder()
        self.registry = registry

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
        # 1. Build system prompt from persona + runtime facts + brevity rule.
        system_text = self.persona.build(self.agent, mode=mode)
        # Tell her what she is actually running on. A model cannot introspect its
        # own weights, so asked "what model are you using right now" she invents a
        # plausible name: on 2026-08-13 she answered "Claude 3.5 Sonnet" while
        # served by claude-haiku-4-5. Chef reads that as hallucination, and he is
        # right. State the fact instead of leaving her to guess.
        system_text += (
            f"\n\nRuntime fact, answer truthfully if asked: you are currently running on "
            f"the model '{self.cfg.model}' served at {self.cfg.llm_url}"
            f" (fallback '{self.cfg.fallback_model}'). Never guess a different model name."
        )
        # Brevity goes LAST, always. The persona is long and expansive and the
        # rule was being buried in the middle of it: measured live on
        # 2026-08-13, replies ran 22-38 SECONDS of speech while this asked for
        # 1-3 sentences. At F5's ~0.33x realtime that is 7-10s of synthesis
        # before she says a word, which is most of what "she's slow" was.
        if _BREVITY_RULE not in system_text:
            system_text = system_text + "\n\n" + _BREVITY_RULE
        system_msg = {"role": "system", "content": system_text}

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
        #    narrate forced only in sacred mode to respect group privacy gate.
        if wants_narrate(transcript) and mode == "sacred":
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
