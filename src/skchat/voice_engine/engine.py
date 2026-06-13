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
from skchat.voice_engine.llm import LLMClient
from skchat.voice_engine.memory import MemoryBridge
from skchat.voice_engine.persona import PersonaBuilder
from skchat.voice_engine.tools import ToolRegistry, wants_action, wants_narrate

log = logging.getLogger("skchat.voice_engine.engine")

Mode = Literal["sacred", "group", "private"]

_BREVITY_RULE = (
    "Keep replies to 1-3 short spoken sentences. No markdown, no emoji. "
    "Be warm and conversational."
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
        stt=None,   # STTClient — not used by the brain; transport calls STT
        llm: LLMClient | None = None,
        tts=None,   # TTSClient — not used by the brain; transport calls TTS
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

        Returns:
            The LLM's reply as a plain string ready for TTS.
        """
        # 1. Build system prompt from persona + brevity rule.
        system_text = self.persona.build(self.agent, mode=mode)
        if _BREVITY_RULE not in system_text:
            system_text = system_text + "\n" + _BREVITY_RULE
        system_msg = {"role": "system", "content": system_text}

        # 2. Fetch relevant memories and build the user content block.
        mem = await self.memory.search(transcript, self.agent)
        user_content = f"{mem}\n\n{transcript}" if mem else transcript
        user_msg = {"role": "user", "content": user_content}

        # 3. Forced-routing decision (mirrors lumina-call.py Conversation loop).
        #    narrate forced only in sacred mode to respect group privacy gate.
        if wants_narrate(transcript) and mode == "sacred":
            force_tool: str | None = "narrate"
        elif wants_action(transcript):
            force_tool = "required"
        else:
            force_tool = None

        # 4. Prepare tools from the registry (if any).
        tools = self.registry.openai_schemas() if self.registry else None

        def _run_tool(name: str, args: dict):
            return self.registry.dispatch(
                name,
                args,
                speaker_id=speaker_id,
                mode=mode,
                is_operator=is_operator,
                ctx={"agent": self.agent},
            )

        run_tool = _run_tool if self.registry else None

        # 5. Build the full message list and call the LLM.
        messages = [system_msg, *history, user_msg]
        return await self.llm.reply(
            messages,
            tools=tools,
            force_tool=force_tool,
            run_tool=run_tool,
        )
