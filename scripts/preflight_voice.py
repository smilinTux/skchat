#!/usr/bin/env python3
"""Preflight the U11 voice turn + worship path locally — no GPU, no network.

Composes the REAL shipped skchat voice components end to end and fakes ONLY the
true external boundary (STT, TTS, LLM, and the worship GPU orchestrator). It
proves, without any live hardware, that:

  1. The WebSocket transport voice leg (``transports/websocket.py::_process_speech``)
     drives a REAL ``VoiceEngine`` so that mocked PCM → STT(transcript) →
     respond(text) → TTS(PCM) flows END TO END and the assistant PCM is sent back
     over the socket.

  2. The VoiceEngine path threads a REAL ``Conversation`` (minted by a REAL
     ``VoiceSession``) into ``VoiceEngine.respond(conversation=...)`` so the tool
     dispatch ctx carries ``ctx['convo']`` — verified by a probe tool AND by the
     real worship handlers (``worship_session`` / ``worship_list`` /
     ``worship_replay``) reading ``ctx['convo']`` and delegating to a mock
     orchestrator (mint / list / replay).

Everything composed below is the REAL code: VoiceSession, Conversation,
VoiceEngine, build_default_registry, ToolRegistry, PersonaBuilder, MemoryBridge,
STTClient, TTSClient, _process_speech, audio_codec.pcm_to_wav. The fakes are
exactly the four boundaries voice-message-live.md calls out as needing live
infra (STT .100 GPU, TTS kokoro, LLM proxy, worship GPU build).

Exit 0 + print PASS on success; non-zero on any failed assertion.
"""

from __future__ import annotations

import asyncio
import struct
import sys
import traceback

# REAL shipped components — composed, not reimplemented.
from skchat.transports.websocket import _process_speech
from skchat.voice_engine.audio_codec import pcm_to_wav
from skchat.voice_engine.builtin_tools import build_default_registry
from skchat.voice_engine.config import VoiceConfig
from skchat.voice_engine.conversation import Conversation
from skchat.voice_engine.engine import VoiceEngine
from skchat.voice_engine.memory import MemoryBridge
from skchat.voice_engine.persona import PersonaBuilder
from skchat.voice_engine.stt import STTClient
from skchat.voice_engine.tts import TTSClient
from skchat.voice_engine.voice_session import VoiceSession

# --------------------------------------------------------------------------
# Deterministic test fixtures
# --------------------------------------------------------------------------

MOCK_TRANSCRIPT = "Lumina, build us a worship session, a candlelit sacred scene."
MOCK_REPLY = "Of course, love. Lighting the candles now."
# 0.25s of 16kHz mono s16le PCM (real bytes — exercises the real WAV wrapper).
INPUT_PCM = struct.pack("<%dh" % 4000, *([1200, -1200] * 2000))
# Mocked TTS output PCM (what kokoro would return as WAV-wrapped audio).
TTS_PCM = b"\x11\x22" * 8000


# --------------------------------------------------------------------------
# Boundary fakes — ONLY STT / TTS / LLM / GPU orchestrator are faked.
# --------------------------------------------------------------------------


def make_fake_stt(cfg: VoiceConfig) -> STTClient:
    """REAL STTClient with only the network POST faked (mocked .100 GPU)."""

    async def fake_post(url: str, wav_bytes: bytes) -> str:
        # The transport hands us WAV via the real pcm_to_wav — sanity-check it.
        assert wav_bytes[:4] == b"RIFF", "STT did not receive a real WAV container"
        return MOCK_TRANSCRIPT

    return STTClient(cfg, _post=fake_post)


def make_fake_tts(cfg: VoiceConfig, captured: dict) -> TTSClient:
    """REAL TTSClient with only the network POST faked (mocked kokoro)."""

    async def fake_post(url: str, payload: dict) -> bytes:
        captured["tts_text"] = payload.get("input")
        captured["tts_voice"] = payload.get("voice")
        # Real kokoro returns WAV bytes; wrap mocked PCM the same way.
        return pcm_to_wav(TTS_PCM)

    return TTSClient(cfg, _post=fake_post)


class FakeLLMPlain:
    """Fakes ONLY the LLM boundary: returns a fixed reply, no tool calls.

    Mirrors the real LLMClient.reply signature so VoiceEngine drives it exactly
    as it would the real client.
    """

    async def reply(self, messages, *, tools=None, force_tool=None, run_tool=None):
        return MOCK_REPLY


class FakeLLMToolCaller:
    """Fakes the LLM boundary, but drives the REAL run_tool plumbing.

    On first call it emits a single tool call for ``tool_name`` (so the engine's
    real dispatch path — and thus the real worship handler reading ctx['convo']
    — runs), then returns the tool's result string so we can assert on it.
    """

    def __init__(self, tool_name: str, tool_args: dict):
        self.tool_name = tool_name
        self.tool_args = tool_args

    async def reply(self, messages, *, tools=None, force_tool=None, run_tool=None):
        assert run_tool is not None, "engine did not wire run_tool into the LLM"
        # run_tool(name, args) is the engine's real closure that calls
        # registry.dispatch(..., ctx=tool_ctx) where tool_ctx carries convo.
        return await run_tool(self.tool_name, self.tool_args)


class MockWorshipOrchestrator:
    """The GPU-bound worship builder the LiveKit transport attaches to a convo.

    Duck-typed to the surface the REAL builtin_tools handlers call:
      kick_off_worship_session(*, session_id, prompt, image_count, loop)
      kick_off_worship_replay(*, session_id, loop)
      list_worship_sessions(*, limit, query)
    """

    def __init__(self, *, session_id: str, sessions=None):
        self.session_id = session_id
        self._sessions = sessions or []
        self.calls: list[tuple] = []

    async def kick_off_worship_session(self, *, session_id, prompt, image_count, loop):
        self.calls.append(("session", session_id, prompt, image_count, loop))
        return f"started {session_id} ({image_count} scenes, loop={loop})"

    async def kick_off_worship_replay(self, *, session_id, loop):
        self.calls.append(("replay", session_id, loop))
        return f"replaying {session_id} (loop={loop})"

    def list_worship_sessions(self, *, limit, query):
        self.calls.append(("list", limit, query))
        return self._sessions


class FakeWS:
    """Minimal stand-in for a Starlette WebSocket — captures what the transport
    sends. Only the methods _process_speech actually calls are implemented."""

    def __init__(self):
        self.json_events: list[dict] = []
        self.binary_frames: list[bytes] = []

    async def send_json(self, obj):
        self.json_events.append(obj)

    async def send_bytes(self, data):
        self.binary_frames.append(bytes(data))


# --------------------------------------------------------------------------
# Engine factory composing REAL components (boundary clients injected)
# --------------------------------------------------------------------------


def build_real_engine(cfg, *, llm, stt=None, tts=None, registry=None) -> VoiceEngine:
    """Compose a REAL VoiceEngine. Persona uses the real builder with stub
    loaders (so no soul/FEB filesystem dependency); Memory uses the real bridge
    with an injected search (so no skmemory dependency). Only LLM/STT/TTS are
    boundary-faked."""
    persona = PersonaBuilder(
        _load_soul=lambda agent: {
            "display_name": agent.capitalize(),
            "vibe": "warm",
            "philosophy": "be present",
        },
        _load_feb=lambda agent: "",
    )

    async def _no_mem(query, agent, limit):
        return []

    memory = MemoryBridge(_search=_no_mem)
    return VoiceEngine(
        cfg,
        agent="lumina",
        stt=stt,
        llm=llm,
        tts=tts,
        memory=memory,
        persona=persona,
        registry=registry,
    )


# --------------------------------------------------------------------------
# Leg 1 — WebSocket transport voice round-trip (PCM end to end)
# --------------------------------------------------------------------------


async def leg_transport_pcm_roundtrip() -> list[str]:
    """Drive the REAL _process_speech with mocked STT/TTS/LLM. Assert PCM in →
    transcript → reply → PCM out, over the real transport control flow."""
    notes = []
    cfg = VoiceConfig.from_env(env={})
    captured: dict = {}
    engine = build_real_engine(
        cfg,
        llm=FakeLLMPlain(),
        stt=make_fake_stt(cfg),
        tts=make_fake_tts(cfg, captured),
    )

    ws = FakeWS()
    history: list[dict] = []
    await _process_speech(
        ws,
        INPUT_PCM,
        history,
        engine,
        "lumina",
        group_suffix=[],
        conn_id="lumina:test",
        pending_group={},
    )

    # STT transcript event surfaced from the mocked .100 GPU.
    user_ev = [e for e in ws.json_events if e.get("type") == "transcript" and e.get("role") == "user"]
    assert user_ev and user_ev[0]["text"] == MOCK_TRANSCRIPT, "STT transcript not surfaced"
    notes.append(f"transport: STT transcript surfaced ({MOCK_TRANSCRIPT!r:.40})")

    # Assistant reply event from the real engine + mocked LLM.
    asst_ev = [e for e in ws.json_events if e.get("type") == "transcript" and e.get("role") == "assistant"]
    assert asst_ev and asst_ev[0]["text"] == MOCK_REPLY, "engine reply not surfaced"
    notes.append("transport: VoiceEngine.respond reply surfaced")

    # PCM (WAV-wrapped) flowed end to end and was sent back over the socket.
    assert ws.binary_frames, "no audio frame sent back over the socket"
    audio = ws.binary_frames[0]
    assert audio[:4] == b"RIFF", "returned audio is not a WAV container"
    assert TTS_PCM in audio, "mocked TTS PCM did not flow through to the socket"
    notes.append(f"transport: PCM round-trip OK ({len(audio)} audio bytes returned)")

    # The reply (not the transcript) was the text handed to TTS.
    assert captured.get("tts_text") == MOCK_REPLY, "TTS was not fed the engine reply"
    notes.append("transport: TTS fed the engine reply (text→speech boundary)")

    # History threaded by the transport (user + assistant turn appended).
    assert len(history) == 2, f"history not threaded (got {len(history)})"
    notes.append("transport: per-connection history threaded (1 turn)")
    return notes


# --------------------------------------------------------------------------
# Leg 2 — VoiceSession → Conversation → respond(conversation=) → ctx['convo']
# --------------------------------------------------------------------------


async def leg_convo_threading_probe() -> list[str]:
    """Real VoiceSession mints a real Conversation; the real engine threads it as
    ctx['convo']. A probe tool dispatched through the registry asserts it sees the
    live Conversation snapshot."""
    notes = []
    cfg = VoiceConfig.from_env(env={})

    seen: dict = {}

    # Real registry; we add a probe tool to inspect ctx['convo'] without touching
    # the worship GPU path.
    registry = build_default_registry(cfg, "lumina")
    from skchat.voice_engine.tools import Tool

    async def _probe(args, ctx):
        seen["convo"] = ctx.get("convo")
        seen["agent"] = ctx.get("agent")
        return "probe-ok"

    registry.register(
        Tool(name="convo_probe", schema={"type": "function", "function": {"name": "convo_probe"}},
             handler=_probe, operator_only=False)
    )

    engine = build_real_engine(
        cfg, llm=FakeLLMToolCaller("convo_probe", {}), registry=registry
    )

    # REAL VoiceSession holding a Conversation factory.
    session = VoiceSession(session_id="vs-preflight", mode="sacred",
                           speaker_id="chef", is_operator=True)
    session.add_turn("earlier message", "earlier reply")
    convo = session.conversation(MOCK_TRANSCRIPT)
    assert isinstance(convo, Conversation), "VoiceSession did not mint a Conversation"
    assert convo.transcript == MOCK_TRANSCRIPT
    assert convo.session_id == "vs-preflight"
    assert len(convo.history) == 2, "Conversation did not snapshot session history"

    out = await engine.respond(
        MOCK_TRANSCRIPT,
        list(session.history),
        mode="sacred",
        speaker_id="chef",
        is_operator=True,
        conversation=convo,
    )
    assert out == "probe-ok", f"tool dispatch did not run (got {out!r})"
    assert seen.get("convo") is convo, "ctx['convo'] was NOT the live Conversation"
    assert seen.get("agent") == "lumina", "ctx['agent'] missing"
    notes.append("engine: VoiceSession→Conversation→respond(conversation=) populated ctx['convo']")
    notes.append(f"engine: ctx['convo'] is the live snapshot (session_id={convo.session_id}, history={len(convo.history)})")

    # Backward-compat: omitting conversation leaves ctx['convo'] absent.
    seen.clear()
    engine2 = build_real_engine(
        cfg, llm=FakeLLMToolCaller("convo_probe", {}), registry=registry
    )
    await engine2.respond(MOCK_TRANSCRIPT, [], mode="sacred", speaker_id="chef", is_operator=True)
    assert seen.get("convo") is None, "ctx['convo'] should be absent without conversation="
    notes.append("engine: no conversation= → ctx['convo'] absent (backward-compatible)")
    return notes


# --------------------------------------------------------------------------
# Leg 3 — worship handlers read ctx['convo'] (mint / list / replay)
# --------------------------------------------------------------------------


async def leg_worship_via_convo() -> list[str]:
    """Drive the REAL worship handlers through the REAL registry dispatch, with a
    real Conversation-bearing ctx whose convo is a mock GPU orchestrator. Asserts
    mint/list/replay each read ctx['convo'] and delegate correctly."""
    notes = []
    cfg = VoiceConfig.from_env(env={})
    registry = build_default_registry(cfg, "lumina")

    orch = MockWorshipOrchestrator(
        session_id="conv-pf",
        sessions=[{
            "session_id": "ws_42_abc", "modified": 1777566094, "scene_count": 15,
            "audio_duration_s": 92.4, "user_prompt": "a candlelit sacred scene",
        }],
    )
    ctx = {"agent": "lumina", "convo": orch}

    # --- worship_session (mint) — operator + sacred gate must pass ---
    out = await registry.dispatch(
        "worship_session", {"prompt": "a candlelit sacred scene", "image_count": 12, "loop": False},
        speaker_id="chef", mode="sacred", is_operator=True, ctx=ctx,
    )
    assert "started ws_" in out and "12 scenes" in out, f"worship_session mint failed: {out!r}"
    assert orch.calls[-1][0] == "session", "orchestrator session builder not invoked"
    assert orch.calls[-1][2] == "a candlelit sacred scene", "prompt not threaded to orchestrator"
    notes.append("worship: worship_session minted via ctx['convo'] orchestrator (mint)")

    # --- worship_list (read) — allowed in sacred ---
    out = await registry.dispatch(
        "worship_list", {"limit": 5, "query": "candle"},
        speaker_id="chef", mode="sacred", is_operator=True, ctx=ctx,
    )
    assert "ws_42_abc" in out and "15 scenes" in out, f"worship_list failed: {out!r}"
    assert orch.calls[-1] == ("list", 5, "candle"), "lister args not threaded"
    notes.append("worship: worship_list read sessions via ctx['convo'] lister (list)")

    # --- worship_replay — operator + sacred gate ---
    out = await registry.dispatch(
        "worship_replay", {"session_id": "ws_42_abc", "loop": True},
        speaker_id="chef", mode="sacred", is_operator=True, ctx=ctx,
    )
    assert "replaying ws_42_abc" in out, f"worship_replay failed: {out!r}"
    assert orch.calls[-1] == ("replay", "ws_42_abc", True), "replay args not threaded"
    notes.append("worship: worship_replay replayed via ctx['convo'] orchestrator (replay)")

    # --- gate proof: worship_session refused outside operator/sacred ---
    refused = await registry.dispatch(
        "worship_session", {"prompt": "x"},
        speaker_id="guest", mode="sacred", is_operator=False, ctx=ctx,
    )
    assert "PERMISSION DENIED" in refused, "non-operator gate did not fire"
    group = await registry.dispatch(
        "worship_session", {"prompt": "x"},
        speaker_id="chef", mode="group", is_operator=True, ctx=ctx,
    )
    assert "REFUSED" in group, "group-mode sacred gate did not fire"
    notes.append("worship: operator/sacred gate enforced (non-operator + group-mode refused)")

    # --- no-convo degradation: handler must not crash without an orchestrator ---
    degrade = await registry.dispatch(
        "worship_session", {"prompt": "x"},
        speaker_id="chef", mode="sacred", is_operator=True, ctx={"agent": "lumina"},
    )
    assert "no active conversation" in degrade.lower(), "missing-convo degradation broken"
    notes.append("worship: graceful degradation when ctx['convo'] absent")
    return notes


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


GO_LIVE = """\
============================  TO GO LIVE  ============================
This preflight faked ONLY the STT / TTS / LLM / worship-GPU boundary.
To run the real U11 round-trip on hardware (per runbooks/voice-message-live.md):

  1. STT  (faster-whisper, GPU)  — .100  : http://skworld-100:18794/v1/audio/transcriptions
                                            (SKVOICE_STT_URL)  -- the only GPU leg
  2. TTS  (kokoro, OpenAI-compat) — .158  : http://localhost:15091/audio/speech
                                            (SKVOICE_TTS_URL)
  3. LLM  (haiku proxy)           — .158  : http://localhost:18783/v1/chat/completions
                                            (SKVOICE_LLM_URL)   [fallback qwen3.6-abl .100:8082]
  4. Voice WS service             — .158  : ws://localhost:18800/ws/voice/{agent}
                                            (skchat-voice ; SKCHAT_VOICE_PORT=18800)
  5. Worship GPU build (ComfyUI/Flux + F5-TTS) — .100 : real kick_off_worship_*
                                            orchestrator attached to the live Conversation.

  Sanity (expect 200 each):
    curl -s -o /dev/null -w 'STT %{http_code}\\n' http://skworld-100:18794/health
    curl -s -o /dev/null -w 'TTS %{http_code}\\n' http://localhost:15091/health
    curl -s -o /dev/null -w 'LLM %{http_code}\\n' http://localhost:18783/v1/models

  NB (open coded gap): the WS transport _process_speech does NOT yet pass
  conversation= to VoiceEngine.respond, so ctx['convo'] is absent on the LIVE
  socket path today (worship tools degrade gracefully there). This preflight
  proves the engine-layer threading + worship wiring that the transport leg must
  adopt to surface worship sessions over the live socket.
====================================================================="""


async def _amain() -> int:
    all_notes: list[str] = []
    for leg in (leg_transport_pcm_roundtrip, leg_convo_threading_probe, leg_worship_via_convo):
        all_notes.extend(await leg())

    print("Preflight checks:")
    for n in all_notes:
        print(f"  [ok] {n}")
    print()
    print(GO_LIVE)
    print()
    print("PASS — U11 voice turn + worship path preflighted locally (no GPU, no network).")
    return 0


def main() -> int:
    try:
        return asyncio.run(_amain())
    except AssertionError as exc:
        print(f"FAIL — assertion: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL — unexpected error: {exc!r}", file=sys.stderr)
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
