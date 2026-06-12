# Unified Voice Engine — Design Spec

**Date:** 2026-06-12
**Status:** Approved (shape) — pending spec review
**Goal:** Merge the two Lumina conversational pipelines (skchat web voice + the LiveKit "skvideo" call) into ONE shared engine living in the `skchat` repo, so there is a single STT→LLM→TTS brain, a single config surface, and the skvideo monolith is finally version-controlled.

---

## 1. Why

Today there are **two independent STT→LLM→TTS implementations** for the same agent:

| | What it is | Transport | Code | In git? | Config |
|---|---|---|---|---|---|
| **skvoice** (`:18800`) | conversational engine | browser WebSocket (text+voice) | `skvoice/` repo, ~1,635 lines, clean package | yes | `skvoice.env` |
| **skvideo** (lumina-call) | conversational engine + avatar | LiveKit WebRTC (voice+video) | `lumina-creative/scripts/lumina-call.py`, **2,958 lines, uncommitted** | **no** | systemd drop-ins |

Consequences felt in practice:
- The **same model setting had to be fixed in two places** (skvoice env *and* lumina-call drop-ins) — the "models pointed to nothing" incident on 2026-06-12 touched both.
- skvideo (the richer pipeline) is a single uncommitted script — no version control, no tests, no review.
- The two diverge in capability: lumina-call has streaming LLM, tool-calling, streaming TTS, FEB-primed persona, private/group modes, VAD/barge-in, and avatar video; skvoice is the stripped batch-only version.

**Key insight from mapping both:** they are the *same loop with different transports*. lumina-call is a **superset** of skvoice. So the merge target = lumina-call's capabilities become the shared engine, and the web chat inherits all of them.

## 2. Decisions (locked)

- **Home:** everything moves into the `skchat` repo. The standalone `skvoice` repo is **retired** (its good code absorbed; optionally a thin deprecation shim left behind so existing imports don't hard-break).
- **Engine = superset:** streaming LLM, tool-calling, fallback chain, streaming TTS, and FEB/soul persona are engine capabilities, toggleable per transport. The web path gains them.
- **Two transports over one engine:** WebSocket (browser) and LiveKit (WebRTC + avatar). Video is a capability of the LiveKit transport, not a separate brain.
- **One config schema:** a single `VoiceConfig` (single source of truth) replaces both `skvoice.env` and the lumina-call env/drop-in sprawl.

## 3. Target structure (in `skchat/src/skchat/`)

```
voice_engine/                 ← the ONE shared brain (no transport code)
  config.py    VoiceConfig — unified env schema (models, URLs, voices, VAD knobs)
  stt.py       STTClient: pcm→wav→transcript; opt-in VAD + whisper-hallucination filter
  llm.py       LLMClient: OpenAI-compat; batch + stream + tool-calling + primary→fallback
  tts.py       TTSClient: batch WAV + streaming PCM (sentence-paced)
  memory.py    MemoryBridge: skmemory search + snapshot (SDK-direct, subprocess fallback)
  persona.py   PersonaBuilder: soul + FEB + ritual → system prompt; private/group modes
  tools.py     shared tool registry (search_memory, narrate, worship, reflections, …)

transports/
  websocket.py   absorbs skvoice service.py — FastAPI :18800, browser turn loop
  livekit.py     absorbs lumina-call.py — WebRTC, per-participant VAD, barge-in, avatar/MuseTalk
```

Two services run the transports:
- `skchat-voice.service` → `transports/websocket.py` (was `skvoice.service`)
- `skchat-call.service`  → `transports/livekit.py` (was `skchat-lumina-call.service`)

Both import `skchat.voice_engine`. One config file, sourced by both.

## 4. Engine interfaces (the contract)

```python
# voice_engine/stt.py
class STTClient:
    def __init__(self, cfg: VoiceConfig): ...
    async def transcribe(self, pcm16k_mono: bytes, *, vad: bool = False) -> str
        # vad=True applies RMS gate + hallucination/repeat filters (lumina behavior);
        # vad=False is the plain batch path (skvoice behavior).

# voice_engine/llm.py
class LLMClient:
    def __init__(self, cfg: VoiceConfig): ...
    async def reply(self, messages: list[Msg], *, tools: list[Tool] | None = None) -> Reply
        # batch; handles tool recursion (cap 4) + primary→fallback on error/empty.
    async def stream(self, messages: list[Msg]) -> AsyncIterator[str]
        # token deltas for fast first-audio; no tools (matches lumina streaming path).

# voice_engine/tts.py
class TTSClient:
    def __init__(self, cfg: VoiceConfig): ...
    async def synthesize(self, text: str, *, voice: str) -> bytes          # batch WAV
    async def stream(self, text: str, *, voice: str) -> asyncio.Queue[bytes|None]  # raw PCM

# voice_engine/persona.py
class PersonaBuilder:
    def build(self, agent: str, *, mode: Literal["private","group"]) -> str
        # soul(active.json→installed) + FEB prime (private only) + ritual + mode rules.

# voice_engine/memory.py
class MemoryBridge:
    async def search(self, query: str, agent: str, limit: int = 3) -> str
    async def snapshot(self, content: str, agent: str, tags: str) -> bool
```

`Msg = {"role": str, "content": str}`. The transports own their session/turn loop and call these.

## 5. What stays transport-specific (NOT in the engine)

- **WebSocket transport:** binary-frame accumulation, `END_OF_SPEECH`/`CLEAR_HISTORY`/`group_init`/`inject_session`/`text_message` control protocol, per-connection state, emotion-analysis hook.
- **LiveKit transport:** per-participant audio-track consumption + energy VAD, barge-in interrupt, broadcast follow-up window, multi-participant mode detection, worship-session orchestration, avatar/MuseTalk video-track pushing, sentence-paced streaming playback.

These are genuinely different and remain in their respective `transports/` modules.

## 6. Phasing (each phase independently testable)

**Phase 1 — Extract the engine.** Build `voice_engine/` as a pure library (superset capabilities), no transport. Unit tests for stt/llm/tts/persona/memory against the live local endpoints (haiku `:18783`, qwen3.6-ablit `:8082`, whisper `:18794`, kokoro-proxy `:15091`). Deliverable: importable, tested engine.

**Phase 2 — Rewire WebSocket.** Port skvoice's `service.py` loop onto the engine as `transports/websocket.py`; stand up `skchat-voice.service`; retire `skvoice.service`. The webui voice/text chat now has streaming + tools + FEB persona. Deliverable: browser chat works end-to-end on the engine.

**Phase 3 — Rehome skvideo.** Refactor `lumina-call.py` into `transports/livekit.py` over the engine; keep VAD/barge-in/avatar/modes; stand up `skchat-call.service`; retire the uncommitted script. Deliverable: the LiveKit call works end-to-end on the engine — and is finally in git.

**Phase 4 — Unify config + link modes.** Collapse both env surfaces into one `VoiceConfig` file sourced by both services. Add the webui "Video" toggle that joins the LiveKit room (same engine/session/memory) and a "back to chat" return. Deliverable: one config, mode-switch UX.

## 7. Risks & mitigations

- **lumina-call is a working 2,958-line monolith with subtle behavior (VAD thresholds, barge-in dwell, worship orchestration, mode ceilings).** Mitigation: Phase 3 is a *mechanical re-home* — preserve the exact transport logic, only swap the inline STT/LLM/TTS/persona calls for engine calls. No behavior changes in the same PR.
- **Regression in the live call Chef uses daily.** Mitigation: phases are independently shippable; keep the old `skchat-lumina-call.service` runnable until `skchat-call.service` is validated, then cut over.
- **skvoice retirement breaking imports.** Mitigation: leave a thin `skvoice` shim re-exporting from `skchat.voice_engine` for one release, or grep+fix all importers in the same PR.
- **Config migration.** Mitigation: Phase 4 ships a migration that reads the old env files and writes the unified one; document the mapping.

## 8. Out of scope (YAGNI)

- No new TTS/STT/LLM providers — reuse the live endpoints.
- No changes to LiveKit signaling/pairing/coturn (separate, already-shipped work).
- No multi-agent fan-out changes beyond preserving the existing group mode.
- No avatar/MuseTalk feature work — just preserve the static-avatar + (optional) MuseTalk paths as-is.

## 9. Success criteria

1. One `voice_engine` package; zero duplicated STT/LLM/TTS logic.
2. Both the browser chat and the LiveKit call run on that one engine.
3. One config file changes a model/voice for both surfaces.
4. The former `lumina-call.py` lives in the `skchat` repo under version control with tests around the engine it uses.
5. Web "Video" toggle hands off to the call seamlessly (same session/memory).
