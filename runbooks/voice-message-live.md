# Live Voice-Message Test — speak → STT → respond → TTS  (Use case U11)

**Status:** CREATE / first authoring · 2026-06-17
**Use case:** U11 — *Voice message: speak → STT → send; receive → TTS* (verification
matrix §3). Currently **LIVE ⏳** (CI-green, not yet run live).
**Owner path:** `skchat` (`feat/sk-spaces`).

This runbook exercises the **unified VoiceEngine over the WebSocket transport**:
`transports/websocket.py::_process_speech` (PCM → STT → `VoiceEngine.respond` → TTS
→ audio frame back). The shipped immutable `Conversation` VO and its threading into
`VoiceEngine.respond(..., conversation=...)` are exercised at the **engine** layer;
see "Current status" for the exact wiring state of the transport leg.

---

## Purpose

Prove, on real infra, the full round-trip a human/agent actually performs:

1. Speak (or feed 16 kHz mono PCM) into a WebSocket voice session.
2. STT (faster-whisper on the **.100 GPU**) turns it into a transcript.
3. `VoiceEngine.respond()` produces a reply (LLM proxy on .158).
4. TTS (kokoro proxy on .158) synthesizes WAV audio.
5. The audio frame is sent back over the same socket.

This is the live counterpart to the CI suite
(`tests/transports/test_websocket.py`, `tests/voice_engine/test_engine.py`,
`tests/voice_engine/test_conversation.py`) which prove the same control flow with a
`FakeEngine` and no network.

---

## Prerequisites

### Services (must be UP)

| Leg | Endpoint (default) | Config key | Host | Verify |
|---|---|---|---|---|
| Voice WS service | `ws://localhost:18800/ws/voice/{agent}` | `SKCHAT_VOICE_PORT=18800` | .158 (this box) | `curl -s http://localhost:18800/health` |
| STT (faster-whisper) | `http://skworld-100:18794/v1/audio/transcriptions` | `SKVOICE_STT_URL` | **.100 GPU** | `curl -s -o /dev/null -w '%{http_code}\n' http://skworld-100:18794/health` |
| TTS (kokoro, OpenAI-compat) | `http://localhost:15091/audio/speech` | `SKVOICE_TTS_URL` | .158 | `curl -s -o /dev/null -w '%{http_code}\n' http://localhost:15091/health` |
| LLM (haiku proxy) | `http://localhost:18783/v1/chat/completions` | `SKVOICE_LLM_URL` | .158 | `curl -s -o /dev/null -w '%{http_code}\n' http://localhost:18783/v1/models` |
| LLM fallback | `http://192.168.0.100:8082/v1/chat/completions` | `SKVOICE_FALLBACK_URL` | .100 | (optional) |

All defaults live in `src/skchat/voice_engine/config.py::VoiceConfig.from_env`.

> **NB — which service owns :18800.** Today port 18800 is served by the **legacy
> `skvoice`** systemd unit (`~/.config/systemd/user/skvoice.service` → `skvoice`
> binary; `/health` reports `"service":"skvoice"`). The skchat-native replacement is
> the `skchat-voice` console-script (`skchat.transports.serve_ws:main`, drop-in on the
> same port, `/health` reports `"service":"skchat-voice"`). **This runbook targets the
> skchat-native `skchat-voice` app** — Step 1 stops `skvoice` and launches
> `skchat-voice` so the `transports/websocket.py` code path is what's under test. If
> you instead want to validate the legacy service, skip Step 1 and point the client at
> the running `skvoice` on 18800 (it predates the unified engine and is **not** the U11
> code under test).

### Hardware

- **.100 GPU** reachable on the tailnet (host `skworld-100` / `100.127.233.81`) for
  faster-whisper STT. STT is the only GPU-dependent leg here; LLM-haiku and kokoro TTS
  run on .158.
- A microphone is **optional** — this runbook drives the socket with a generated /
  recorded PCM file so it is deterministic and CI-host-independent. A live mic test is
  the optional Step 6.

### Tokens / creds

- None. The WS voice service is unauthenticated on the tailnet; no bot tokens, no
  capauth signing on this path (operator gate is hard-coded `is_operator=True` in
  `_process_speech` — see "Current status", note on speaker auth).

### Python

- `~/.skenv/bin/python` with `websockets` available (pull-in of the skchat venv).
  Verify: `~/.skenv/bin/python -c "import websockets; print(websockets.__version__)"`.

---

## Setup commands

Run from `~` (never from `smilintux-org/` — skmemory namespace collision; see CLAUDE.md).

```bash
# 0. Sanity: confirm every backend leg is reachable BEFORE touching the WS service.
curl -s -o /dev/null -w 'STT  %{http_code}\n' http://skworld-100:18794/health
curl -s -o /dev/null -w 'TTS  %{http_code}\n' http://localhost:15091/health
curl -s -o /dev/null -w 'LLM  %{http_code}\n' http://localhost:18783/v1/models
# Expect: STT 200, TTS 200, LLM 200.  Any non-200 = fix that leg first (this is the
# difference between a real U11 pass and a fake one).
```

```bash
# 1. Launch the skchat-native voice service on :18800 (the U11 code under test).
#    Stop the legacy skvoice first so they don't fight over the port.
systemctl --user stop skvoice.service 2>/dev/null || true

#    Foreground run (Ctrl-C to stop) — env is read by VoiceConfig.from_env:
cd ~ && \
  SKCHAT_VOICE_PORT=18800 \
  SKVOICE_AGENT=lumina \
  ~/.skenv/bin/skchat-voice
# Leave this running in its own terminal. In another terminal, continue below.
```

```bash
# 2. Confirm the skchat-native service answered (note the service name in the JSON).
curl -s http://localhost:18800/health
# Expect: {"status":"ok","service":"skchat-voice"}   <-- skchat-voice, NOT skvoice
```

---

## Step-by-step procedure

The client speaks the control protocol documented at the top of
`src/skchat/transports/websocket.py`:

- binary frames = raw 16-bit PCM (16 kHz mono), accumulated;
- `"END_OF_SPEECH"` = flush buffer → STT → `VoiceEngine.respond` → TTS → audio back;
- `{"type":"text_message","text":"…"}` = skip STT, straight to respond + TTS.

### Step 1 — Generate a deterministic speech clip (no mic needed)

Use a TTS-rendered clip as the "spoken" input so the test is repeatable. Render WAV
from the live kokoro TTS, then strip to raw 16 kHz mono PCM.

```bash
cd ~
curl -s -X POST http://localhost:15091/audio/speech \
  -H 'content-type: application/json' \
  -d '{"model":"tts-1","input":"What is on my calendar today?","voice":"lumina","response_format":"wav"}' \
  -o /tmp/u11_say.wav
# Down-sample to 16k mono s16le PCM (matches the WS binary-frame contract):
ffmpeg -y -i /tmp/u11_say.wav -ar 16000 -ac 1 -f s16le /tmp/u11_say.pcm 2>/dev/null
ls -l /tmp/u11_say.pcm
```

**Expected observation:** `/tmp/u11_say.pcm` exists and is non-empty (tens of KB). If
`/tmp/u11_say.wav` is empty, the TTS leg is down — stop and fix Setup step 0.

### Step 2 — Drive the voice path (PCM → STT → respond → TTS)

```bash
cd ~ && ~/.skenv/bin/python - <<'PY'
import asyncio, json, websockets

URL = "ws://localhost:18800/ws/voice/lumina"
PCM = open("/tmp/u11_say.pcm", "rb").read()
CHUNK = 32000  # ~1s of 16k mono s16le

async def main():
    async with websockets.connect(URL, max_size=None) as ws:
        # stream PCM as binary frames
        for i in range(0, len(PCM), CHUNK):
            await ws.send(PCM[i:i+CHUNK])
        await ws.send("END_OF_SPEECH")
        audio_bytes = 0
        async for msg in ws:
            if isinstance(msg, bytes):
                audio_bytes += len(msg)
                print(f"[audio] +{len(msg)} bytes (total {audio_bytes})")
                continue
            evt = json.loads(msg)
            print("[event]", evt)
            # 'listening' status AFTER we've seen the assistant transcript = turn done
            if evt.get("type") == "status" and evt.get("state") == "listening" and audio_bytes:
                break
        print(f"\nRESULT: assistant audio bytes returned = {audio_bytes}")

asyncio.run(main())
PY
```

**Expected observation (event order, from `_process_speech`):**

```
[event] {'type': 'status', 'state': 'processing'}
[event] {'type': 'transcript', 'role': 'user', 'text': 'What is on my calendar today?'}   # STT result
[event] {'type': 'status', 'state': 'thinking'}
[event] {'type': 'transcript', 'role': 'assistant', 'text': '<reply>'}                     # VoiceEngine.respond
[event] {'type': 'status', 'state': 'speaking'}
[audio] +N bytes ...                                                                       # TTS WAV frame
[event] {'type': 'status', 'state': 'listening'}
RESULT: assistant audio bytes returned = <nonzero>
```

- The **user transcript** should approximate the spoken clip ("…calendar today?").
  faster-whisper may differ slightly — that's fine.
- If STT returns empty, `_process_speech` short-circuits to
  `{'state':'listening'}` with **no** assistant transcript and **no** audio. That is a
  **STT FAIL** (silence gate or .100 GPU down), not a pass.

### Step 3 — Drive the text path (skip STT; respond → TTS)

Isolates the LLM+TTS legs from STT, so a failure can be localized.

```bash
cd ~ && ~/.skenv/bin/python - <<'PY'
import asyncio, json, websockets
URL = "ws://localhost:18800/ws/voice/lumina"
async def main():
    async with websockets.connect(URL, max_size=None) as ws:
        await ws.send(json.dumps({"type":"text_message","text":"Say hello in one short sentence."}))
        audio=0
        async for msg in ws:
            if isinstance(msg, bytes):
                audio += len(msg); print(f"[audio] +{len(msg)} (total {audio})"); continue
            evt=json.loads(msg); print("[event]", evt)
            if evt.get("state")=="listening" and audio: break
        print("RESULT text-path audio bytes =", audio)
asyncio.run(main())
PY
```

**Expected observation:** `thinking` → assistant `transcript` → `speaking` → audio
bytes → `listening`. Nonzero audio bytes = LLM + TTS legs healthy independent of STT.

### Step 4 — History continuity (multi-turn)

Send two `text_message`s on the **same** connection; the second reply should reflect
that history is being threaded (`history` list grows; capped at 40→30 in
`_process_speech`/`_process_text`).

```bash
cd ~ && ~/.skenv/bin/python - <<'PY'
import asyncio, json, websockets
URL="ws://localhost:18800/ws/voice/lumina"
async def turn(ws, text):
    await ws.send(json.dumps({"type":"text_message","text":text}))
    async for msg in ws:
        if isinstance(msg, bytes): continue
        evt=json.loads(msg)
        if evt.get("type")=="transcript" and evt.get("role")=="assistant":
            print(f"  reply: {evt['text']!r}")
        if evt.get("state")=="listening": return
async def main():
    async with websockets.connect(URL, max_size=None) as ws:
        print("turn 1:"); await turn(ws, "My name is Chef. Remember it.")
        print("turn 2:"); await turn(ws, "What is my name?")
asyncio.run(main())
PY
```

**Expected observation:** turn 2's reply references "Chef" → per-connection `history`
is carried across turns (the transport's `histories[conn_id]` dict).

### Step 5 — Latency measurement

Wrap Step 2 with a wall clock from `END_OF_SPEECH` to first audio byte.

```bash
# Add to the Step-2 script: record time.monotonic() right after sending
# "END_OF_SPEECH", and again on the first `isinstance(msg, bytes)` frame; print the delta.
```

**Expected latency (warm services, ~1-2s clip):**

| Leg | Budget |
|---|---|
| STT (.100 faster-whisper, base model) | 0.4–1.5 s |
| LLM (haiku proxy, ≤200 tok) | 0.5–2.0 s |
| TTS (kokoro WAV, one short reply) | 0.3–1.0 s |
| **END_OF_SPEECH → first audio byte** | **target < 4 s** |

First request after a cold STT model load may be slower (model warm-up) — discard the
first run, measure the second.

### Step 6 — (Optional) Live microphone

Replace the generated PCM with a real mic capture, same socket path:

```bash
# 3 seconds of mic → 16k mono s16le PCM, then reuse the Step-2 client.
arecord -f S16_LE -r 16000 -c 1 -d 3 -t raw /tmp/u11_mic.pcm
# then in the Step-2 script, read /tmp/u11_mic.pcm instead of /tmp/u11_say.pcm.
```

**Expected observation:** the user transcript matches what you actually said; reply
audio plays if piped to `aplay -f S16_LE -r 16000` (the returned frame is WAV — strip
the header or play via a WAV-aware player).

---

## Pass / Fail criteria

**PASS (U11 → LIVE ✅)** requires **all** of:

1. `skchat-voice` `/health` reports `"service":"skchat-voice"` on :18800 (Setup Step 2).
2. **Voice path (Step 2):** a non-empty **user** transcript event appears (STT
   succeeded against the .100 GPU), followed by an **assistant** transcript and
   **nonzero** audio bytes. Event order matches `_process_speech`.
3. **Text path (Step 3):** assistant transcript + nonzero audio bytes (LLM + TTS legs
   isolated-healthy).
4. **History (Step 4):** turn-2 reply reflects turn-1 context.
5. **Latency (Step 5):** END_OF_SPEECH → first audio byte under ~4 s on a warm second run.

**FAIL** if any of:

- `/health` still reports `"service":"skvoice"` → you tested the legacy service, not
  the U11 code path. Re-do Setup Step 1.
- Step 2 ends at `{'state':'listening'}` with **no** user transcript → STT failed
  (silence gate `SKVOICE_STT_MIN_RMS`, or .100 GPU/STT endpoint down). Confirm Setup
  Step 0 STT=200; lower-bound check: the clip must contain real speech energy.
- Assistant transcript present but **zero** audio bytes → TTS leg failed
  (`_synthesize` returned `b""`; check kokoro :15091).
- Connection error / no events → WS service not running or wrong port.

---

## Current status (CI-proven vs needs-this-run vs gated)

| Leg | State | Evidence |
|---|---|---|
| WS control protocol (text path, CLEAR_HISTORY, unknown-JSON ignored) | **CI-proven** | `tests/transports/test_websocket.py` (FastAPI TestClient + FakeEngine) — 3/3 pass |
| `VoiceEngine.respond` turn orchestration (persona→memory→LLM→tools, forced-routing) | **CI-proven** | `tests/voice_engine/test_engine.py`, `test_llm_tools.py` |
| `Conversation` VO (frozen dataclass, `to_dict`, defaults) + `respond(conversation=…)` signature + `ctx['convo']` threading | **CI-proven** | `tests/voice_engine/test_conversation.py` (6 cases); engine wires `tool_ctx["convo"]` only when `conversation` is passed |
| STT client (faster-whisper POST, VAD energy gate, hallucination filter) | **CI-proven** | `tests/voice_engine/test_stt.py` (mocked POST) |
| TTS client (OpenAI `/audio/speech`, batch + stream) | **CI-proven** | `tests/voice_engine/test_tts.py` (mocked POST/stream) |
| **Full live voice round-trip on real STT(.100)+LLM+TTS** | **NEEDS THIS RUN** | no live proof yet — U11 is **LIVE ⏳** in the matrix |
| **`skchat-voice` on :18800 (vs legacy skvoice)** | **NEEDS THIS RUN** | :18800 currently served by legacy `skvoice`; the skchat-native `serve_ws:main` drop-in is not the running unit yet |
| **Conversation VO actually threaded *through the transport*** | **GAP (not yet wired)** | `_process_speech`/`_process_text` call `engine.respond(llm_input, history, mode=…, speaker_id="chef", is_operator=True)` **without** `conversation=`. The VO + engine threading shipped (wave-1 Task 6), but the WebSocket transport does **not** construct/pass a `Conversation` per turn. So `ctx['convo']` is **absent** on this path today. Wiring it is the transport-side follow-up. |
| **Per-turn speaker auth** | **STUBBED** | `_process_speech` hard-codes `speaker_id="chef", is_operator=True` (comment: "Phase 3 adds proper auth"). Operator gate is effectively always-true on this path. |
| **Worship tool handlers reading `ctx['convo']` (`worship_session`/`worship_list`/`worship_replay`)** | **GATED — wave-5** | `voice_engine/builtin_tools.py` handlers are Phase-3 stubs returning "requires Phase-3 transport integration"; they depend on the Conversation object being threaded through the transport (the gap above) AND on a real `_worship_list_summaries()` injection. Not exercised by this runbook. |
| **Streaming GPU inference (token-stream LLM + PCM-stream TTS, <2s)** | **GATED — wave-5 / GPU** | roadmap U11 Phase 4 (`scripts/tier5_verify_voice.py`, real SKVoice/worship on .100). `TTSClient.stream()` exists + is CI-tested, but this runbook validates the **batch** WAV path; the streaming low-latency loop is a separate gated run. |

**Bottom line:** this runbook flips the **batch** speak→STT→respond→TTS loop from
LIVE ⏳ to LIVE ✅ once observed end-to-end. It does **not** cover (a) the
Conversation-VO-through-transport wiring (a coded gap to close first if you want
`ctx['convo']` populated live), (b) worship tools reading `ctx['convo']` (wave-5,
gated), or (c) streaming GPU inference (wave-5 Phase 4). Record results as a new
finding (F-7) in `docs/qa/skworld-comms-verification-matrix.md §5` and flip the U11 row.
