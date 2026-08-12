# Voice transport consolidation: one mind, one room model

Design doc. Written 2026-08-12 after Chef reported "she's not responding and I
don't see her avatar" in a live call, and asked why there are two different URIs
and whether skvoice should fold into skchat.

Architecture reviewed independently against all three repos before writing.

## The symptom

Chef sits in a LiveKit call. No avatar, no audio, Lumina does not hear him and
does not speak. Nothing errors. Every service reports healthy.

## What is actually there

Three parallel ways an agent talks to a human, sharing neither a room model nor
a pipeline:

1. **skvoice `:18800`** websockets: `/ws/voice/{agent}`, `/ws/video/{agent}`,
   `/ws/facetime/{agent}`. One pipeline (PCM -> Whisper -> LLM -> Piper), three
   URIs; facetime only differs by a 12-byte binary framing. skchat proxies to it
   from `voice_ws_lite.py` and `facetime.py`.
2. **`lumina-creative/scripts/lumina-call.py`**: the full agent (10 MCP servers,
   110 tools, avatar track). Its room is a *deployment* pin, not a code
   hardcode: `--room`, default `SKCHAT_LIVEKIT_DEFAULT_ROOM=lumina-and-chef`.
   The unit is disabled today.
3. **skchat's own call stack**: `call_session.derive_room()` computes a
   deterministic per-pair room, `/call/{start,answer,incoming}` carry signed
   invites, and `call_answerer.py` joins the room an invite names.

## The fault

skchat owns the room model **and** owns a purpose-built, transport-free brain at
`src/skchat/voice_engine/`. But every deployed brain lives somewhere else and
none of them consumes skchat's room model. A re-home into
`src/skchat/transports/livekit.py` was started and stalled halfway: the pure
decision logic landed (`VADSegmenter`, `BargeInDetector`, `AddressingGate`,
`TranscriptDedup`, `run_turn`, `mode_ceiling`) but the room loop its own
docstring promises, `run_agent` / `build_room_session`, was never written.

So today **the thing that joins the right room cannot converse, and the thing
that can converse is not in the room.** `call_answerer._join_and_publish`
publishes literal silence frames by design, its docstring saying it deliberately
avoids importing "that file's heavyweight conversational agent ... which is
coupled to a hardcoded room."

Chef's call landed in `call-e4qj4kxvef2dxmxq` (derived). The agent, when run,
sits in `lumina-and-chef`. They are never in the same room. That is the whole
bug.

## Target architecture

- **Room model stays in skchat.** `derive_room` + signed CALL_INVITE is
  identity-aware (capauth FQIDs). Nothing else ever computes a room.
- **The mind is `skchat.voice_engine`.** It already defines the seam: a
  transport hands in `(speaker_id, transcript)` and calls `engine.respond(...)`,
  `engine.stt`, `engine.tts`. `run_turn()` is exactly this seam and already
  exists.
- **skvoice stays a separate service.** The "heavy deps" argument for folding it
  in is wrong: skvoice HTTP-calls STT (`:18794`), TTS (`:18797`) and the LLM, so
  the weight already lives behind ports. The real reasons to keep it are process
  isolation and that it is a genuine no-SPOF fallback: it needs no SFU, no TURN
  and no UDP, so it survives when WebRTC cannot connect. It was wrongly marked
  deprecated once already while live and load-bearing; do not repeat that.
- **The per-call conversational loop lives in the answerer process**, because
  that is the process the room model already summons.

## The transports, after consolidation

| transport | verdict |
|---|---|
| LiveKit derived rooms | primary, all 1:1 and group calls |
| skvoice `/ws/voice` | keep, the no-SFU/no-UDP fallback and multi-agent group path |
| `/ws/video` | alias to `/ws/voice`; same loop, redundant URI |
| `/ws/facetime` | retire; folding optional avatar frames into the voice protocol as a message type |

End state: two transports, one mind.

## Migration order

Each step leaves the system working.

1. **Write the room loop** in `transports/livekit.py` (`build_room_session`,
   `run_agent`), porting the LiveKit primitives from `lumina-call.py`. Pure
   addition, nothing deployed changes.
2. **Flag-gate an engine-backed session in `call_answerer`**
   (`SKCHAT_ANSWERER_ENGINE=1`, silence loop as the fallback). **This alone
   fixes the mute-agent bug.** New deps on the answerer host are livekit +
   httpx only.
3. **Fix the mode ceiling before flipping the flag** (see traps).
4. Retire `skchat-lumina-call.service`. Keep `lumina-call.py` invocable with
   `--room` until the data-channel `speak`/`worship_done` commands and the
   avatar track are ported, then archive. Skipping this breaks the worship flow
   and the Spaces greeting.
5. Later: point the voice pages at the engine's WS transport, or teach skvoice
   to use the engine. Port `group_init`/`group_context` first or group voice
   breaks. Then retire `/facetime`.

## Traps

- **Mode ceiling keys off the room NAME.** `mode_ceiling()` maps the literal
  string `lumina-and-chef` to "sacred". Derived rooms are opaque hashes, so
  every 1:1 with Chef silently downgrades to the "group" register and Chef-only
  tool auth may misfire. **Key the mode off the invite's verified peer FQID, not
  the room name.** This must land before step 2 is enabled.
- **The answerer can only handle one call.** `run_answerer` calls
  `asyncio.run(...)` inside its poll loop, so during a call it stops polling and
  a second caller rings into nothing.
- **Two WS voice paths inside skchat** (`voice_ws_lite` proxy vs
  `transports/websocket.py`) can drift; `webui.py` currently prefers the proxy.
- **Operator-token spread.** `/livekit/token` is operator-gated
  (`_gate_token_mint`); `lumina-call.py` crash-looped 401 on exactly this. An
  engine-backed answerer inherits the same failure mode.
- **skvoice `tools.py` is dead code**, imported nowhere. Its README claims a
  tool-using turn that does not happen; do not plan parity against it.

## Separately found, needs its own fix

While diagnosing, 15 CALL_INVITE envelopes were stuck in Lumina's inbox with
`from_fqid == to_fqid == lumina@chef.skworld.io`, ringing Chef repeatedly.
`/call/incoming` re-reads the inbox every poll and **nothing ever consumes an
invite**, so an answered call keeps ringing forever. Two defects: invites must be
consumed on answer/decline, and a self-addressed invite should be rejected at
creation.
