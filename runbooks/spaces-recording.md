# Live-test runbook — SK Spaces recording (U7)

Audio-only room-composite **Egress** for a Space, gated by **per-speaker consent**.
Off by default; the ledger gate is enforced server-side. This runbook drives the
full chain on the live `.158` stack and tells you exactly which legs are
CI-proven, which need this live run, and which are gated on a LiveKit **Egress
worker** (not yet deployed).

> **Honesty up front (read this):** the routes, the consent gate, the registry
> `recording` flag, and the **transcript → write-up → chat-lane** pipeline are all
> wired and (the write-up chain) verified LIVE (matrix F-4). What is **NOT yet
> proven end-to-end** is the actual **OGG egress from a live Space** —
> `POST /record/start` calls LiveKit `start_room_composite_egress`, which needs a
> running **Egress worker** connected to the SFU over Redis. No egress worker runs
> on this box today (the SFU is remote at `noroc2027.tail204f0c.ts.net:8443`).
> So: consent gate + start/stop API + write-up = testable now; a real recorded OGG
> file = **gated** until an egress worker is deployed.

---

## Code paths (verified to exist)

| Concern | Symbol / file |
|---|---|
| Egress start/stop | `Recorder.start()` / `Recorder.stop()` — `src/skchat/spaces/recording.py` (`RoomCompositeEgressRequest(audio_only=True, OGG)`) |
| Consent ledger | `ConsentLedger.add/revoke/has` + `can_record(speakers, space_id, ledger)` — `src/skchat/spaces/consent.py` |
| Live-now registry | `SpaceRegistry.live()` / `.set_recording()` / `.add_speaker()` — `src/skchat/spaces/registry.py` |
| Routes | `register_spaces_routes()` — `src/skchat/spaces/routes.py` (`/consent`, `/record/start`, `/record/stop`, `/invite`, `/raise-hand`) |
| Auto write-up hook | `_maybe_start_writeup()` (in `routes.py`) → `RecordingWriteup.process()` — `src/skchat/spaces/recording_writeup.py` |
| Route registration into webui | `webui.py` calls `register_spaces_routes(app)` |
| UI ● REC indicator | `src/skchat/static/spaces.html` (`s.recording`), `src/skchat/static/space.html` (`#rec`) |
| Ledger JSON | `~/.skchat/spaces-consent.json` |
| Registry JSON | `~/.skchat/spaces.json` |
| Recording output dir | `~/.skchat/spaces-recordings/<space_id>.ogg` |

Tests: `tests/test_spaces_recorder.py`, `tests/test_spaces_recording_routes.py`,
`tests/test_spaces_consent.py`, `tests/test_spaces_consent_ledger.py`,
`tests/test_recording_writeup.py`.

---

## Purpose

Verify on real infra that a host can:
1. Have speakers record their **consent** (`/spaces/{id}/consent`).
2. Start recording only when **every on-stage speaker has consented** — and get a
   `409 missing_consent` when one hasn't.
3. See the registry flip `recording=true` and the `● REC` indicator show.
4. Stop recording and (with `SKCHAT_SPACES_AUTO_WRITEUP=1`) get a
   transcript → LLM write-up posted back to the Space's **chat lane**.
5. Confirm the **promote-while-recording** consent gate: promoting a
   non-consenting identity while recording is active is reverted with a `409`.

---

## Prerequisites

**Services**
- `skchat-webui@lumina.service` running on `:8765` (host `0.0.0.0`).
  `systemctl --user status skchat-webui@lumina.service`
- A reachable LiveKit SFU. Live config:
  `SKCHAT_LIVEKIT_URL=wss://noroc2027.tail204f0c.ts.net:8443`,
  `SKCHAT_LIVEKIT_API_KEY=skchat-lumina`, `SKCHAT_LIVEKIT_API_SECRET=<set>`
  (all already in `~/.config/skchat/webui-lumina.env`).
- **LiveKit Egress worker** — REQUIRED for a real OGG to be produced. It must be
  deployed alongside the SFU and share its **Redis**. **Status: not deployed on
  this box.** Without it, `/record/start` will return an error from the LiveKit
  API (or no file appears). See "Current status" — the consent/API/write-up legs
  do NOT need the worker; only the actual-file leg does.

**Env (already set live in `webui-lumina.env`)**
- `SKCHAT_SPACES_AUTO_WRITEUP=1` — fires the write-up pipeline on `/record/stop`.
- `SKCHAT_STT_URL=http://skworld-100:18794/v1/audio/transcriptions` — Whisper STT
  (the write-up transcriber reuses `skchat.voice.VoiceRecorder`).
- `SKCHAT_LLM_URL=http://localhost:18783/v1/chat/completions`,
  `SKCHAT_LLM_MODEL=claude-haiku-4-5` — the write-up summarizer.
- `SKCHAT_LIVEKIT_API_KEY` / `SKCHAT_LIVEKIT_API_SECRET` exported into the webui.

**Tools on the test host**: `curl`, `jq`, `python` (`~/.skenv/bin/python`).

**Hardware**: none extra for the API/consent legs. A real recorded Space needs the
SFU + Egress worker reachable on the tailnet.

---

## Setup

```bash
# 1. Confirm the webui is up and serving spaces routes.
systemctl --user status skchat-webui@lumina.service --no-pager
curl -s http://localhost:8765/health | jq .

# 2. Confirm the LiveKit creds + auto-writeup are loaded into the process.
systemctl --user show skchat-webui@lumina.service -p EnvironmentFiles
grep -E 'AUTO_WRITEUP|LIVEKIT_URL|LIVEKIT_API_KEY|STT_URL|LLM_URL' \
  ~/.config/skchat/webui-lumina.env

# 3. Pick a throwaway space id + host fqid for this run.
export BASE=http://localhost:8765
export HOST_FQID="lumina@skworld.io"
export SLUG="rec-test-$(date +%s)"
```

> The `/spaces/create` route mints a HOST token and requires LiveKit creds
> (`503 livekit not configured` otherwise). It does NOT contact the SFU at create
> time (the room is auto-created when the host first connects), so you can drive
> the consent/registry legs without a browser. The egress leg DOES contact the SFU.

---

## Procedure

### Step 1 — Create the Space

```bash
SID=$(curl -s -X POST "$BASE/spaces/create" \
  -H 'Content-Type: application/json' \
  -d "{\"host_fqid\":\"$HOST_FQID\",\"title\":\"Rec Test\",\"slug\":\"$SLUG\"}" \
  | tee /tmp/space.json | jq -r .space_id)
echo "space_id=$SID"
```
**Expected:** HTTP 200; JSON with `space_id`, `room`, `url`, `role:"host"`,
`token`. `$SID` is non-empty. (If `503 livekit not configured`, the webui env is
missing creds — fix env and restart the unit.)

### Step 2 — Confirm it is in the live-now registry

```bash
curl -s "$BASE/spaces" | jq --arg s "$SID" '.spaces[] | select(.space_id==$s)'
```
**Expected:** the space object appears with `"recording": false` and
`"speakers": []`. (Backed by `SpaceRegistry.live()`.)

### Step 3 — Put a speaker on stage (host invite)

The record gate uses the **server-authoritative** on-stage set (`space.speakers`),
populated by `/invite` or `/raise-hand` via `_on_promoted` → `reg.add_speaker`.
Use `/invite` so we have a real on-stage speaker to consent-gate.

```bash
SPEAKER="alice@skworld.io"
curl -s -X POST "$BASE/spaces/$SID/invite" \
  -H 'Content-Type: application/json' \
  -d "{\"requester\":\"$HOST_FQID\",\"identity\":\"$SPEAKER\"}" | jq .
curl -s "$BASE/spaces" | jq --arg s "$SID" '.spaces[] | select(.space_id==$s) | .speakers'
```
**Expected:** `{"ok":true,"on_stage":true}` and `speakers` now contains
`alice@skworld.io`.
> NOTE: `/invite` calls the LiveKit Moderator (`stage_action`) against the SFU. If
> the SFU is unreachable this step errors — that is an infra problem, not a recording
> bug. For a pure-API consent test without the SFU you can instead seed
> `~/.skchat/spaces.json` `speakers` directly, but the live path is `/invite`.

### Step 4 — Try to start recording BEFORE consent (must 409)

```bash
curl -s -o /tmp/r.json -w '%{http_code}\n' -X POST "$BASE/spaces/$SID/record/start" \
  -H 'Content-Type: application/json' -d "{\"requester\":\"$HOST_FQID\"}"
jq . /tmp/r.json
```
**Expected:** HTTP **409**; body `{"ok":false,"missing_consent":["alice@skworld.io"]}`.
This proves `can_record()` gates on the authoritative speaker set, not a
body-supplied list.

### Step 5 — Record the speaker's consent

```bash
curl -s -X POST "$BASE/spaces/$SID/consent" \
  -H 'Content-Type: application/json' \
  -d "{\"identity\":\"$SPEAKER\"}" | jq .
jq --arg s "$SID" '.[$s]' ~/.skchat/spaces-consent.json
```
**Expected:** `{"ok":true}`; the ledger JSON now lists `alice@skworld.io` under
this space id. (Backed by `ConsentLedger.add`.)

### Step 6 — Start recording (now should pass the gate)

```bash
curl -s -o /tmp/r2.json -w '%{http_code}\n' -X POST "$BASE/spaces/$SID/record/start" \
  -H 'Content-Type: application/json' -d "{\"requester\":\"$HOST_FQID\"}"
jq . /tmp/r2.json
```
**Expected (egress worker present):** HTTP **200**;
`{"ok":true,"egress_id":"EG_...","path":".../spaces-recordings/<sid>.ogg"}`.
Then:
```bash
curl -s "$BASE/spaces" | jq --arg s "$SID" '.spaces[] | select(.space_id==$s) | .recording'
```
→ `true`, and the `● REC` indicator renders in `spaces.html` / `space.html`.

**Expected (NO egress worker — the current state):** the consent gate has already
passed (no 409). The LiveKit egress call itself fails — typically a 5xx / API error
surfaced from `Recorder.start()` because no Egress worker is connected to the SFU's
Redis. **This is the gated leg.** Record the exact error you see; the consent +
gate logic above is what U7 needs proven, and it is.

### Step 7 — Confirm the promote-while-recording consent gate (I3)

(Only meaningful if Step 6 set `recording=true`.) Try to promote a *second*,
non-consenting identity while recording is active:
```bash
curl -s -o /tmp/p.json -w '%{http_code}\n' -X POST "$BASE/spaces/$SID/invite" \
  -H 'Content-Type: application/json' \
  -d "{\"requester\":\"$HOST_FQID\",\"identity\":\"bob@skworld.io\"}"
jq . /tmp/p.json
```
**Expected:** HTTP **409** `"consent required to speak while recording is active"`,
and `bob@skworld.io` is NOT in `speakers` (the promotion was reverted via a
`remove` stage action — see `_on_promoted`).

### Step 8 — Stop recording (fires the write-up)

```bash
curl -s -X POST "$BASE/spaces/$SID/record/stop" \
  -H 'Content-Type: application/json' -d "{\"requester\":\"$HOST_FQID\"}" | jq .
curl -s "$BASE/spaces" | jq --arg s "$SID" '.spaces[] | select(.space_id==$s) | .recording'
```
**Expected:** `{"ok":true,"writeup_started":true}` (because
`SKCHAT_SPACES_AUTO_WRITEUP=1`), and `recording` → `false`.

### Step 9 — Verify the write-up landed in the chat lane

The write-up runs in a daemon thread (Whisper + LLM are slow). Give it time, then
replay the chat lane:
```bash
sleep 20
curl -s "$BASE/spaces/$SID/lanes/chat/state" | jq '.events[-1]'
```
**Expected:**
- If a real OGG with speech exists → an event whose `text` is a markdown write-up
  with `## Summary` / `## Key Points` / `## Action Items`, `from` = the agent.
- If the recording was silent / no transcript → the honest
  `"No audio / no transcript was produced …"` note. Either is a PASS for the
  write-up pipeline (it always posts something).
- Logs: `journalctl --user -u skchat-webui@lumina -n 50 | grep -i writeup`.

### Step 10 — Clean up

```bash
curl -s -X POST "$BASE/spaces/$SID/end" \
  -H 'Content-Type: application/json' -d "{\"requester\":\"$HOST_FQID\"}" | jq .
```
**Expected:** `{"ok":true}`; the space drops out of `GET /spaces` (status `ended`).

---

## Pass/Fail criteria

| # | Leg | PASS condition | Needs egress worker? |
|---|-----|----------------|----------------------|
| 1 | Create + registry | space appears in `GET /spaces`, `recording:false` | no |
| 2 | On-stage authoritative set | `/invite` adds speaker to `speakers` | no (needs SFU for stage_action) |
| 3 | **Consent gate (negative)** | `/record/start` → **409 `missing_consent`** before consent | no |
| 4 | Consent ledger | `/consent` writes identity to `spaces-consent.json` | no |
| 5 | **Start passes gate** | `/record/start` no longer 409 (gate satisfied) | no (the *gate*); yes for the OGG |
| 6 | Egress produces OGG | `recording:true`, `<sid>.ogg` exists, `egress_id` set | **YES** |
| 7 | **Promote-while-recording gate** | non-consenting `/invite` → **409**, not promoted | no (needs SFU) |
| 8 | Stop | `/record/stop` → `recording:false`, `writeup_started:true` | no |
| 9 | **Write-up posted** | chat lane last event is a write-up or honest no-transcript note | no |

**Overall U7 PASS for this run** = legs 1,3,4,5(gate),7,8,9 green. Leg 6 (real OGG)
is a separate PASS that requires the egress worker.

---

## Current status

| Leg | Level | Notes |
|---|---|---|
| Recorder unit (egress req shape, start/stop) | **CI** | `test_spaces_recorder.py` (9 cases w/ injected egress) |
| Consent ledger + `can_record` gate | **CI** | `test_spaces_consent.py`, `test_spaces_consent_ledger.py` (15 cases) |
| Recording routes (start/stop/409 gate) | **CI** | `test_spaces_recording_routes.py` |
| Promote-while-recording 409 (I3) | **CI** | covered in moderation/recording route tests |
| Live-now registry `recording` flag | **CI** | `test_spaces_registry.py` |
| Recording write-up orchestrator (seams) | **CI** | `test_recording_writeup.py` (5 cases, faked Whisper/LLM/net) |
| **Write-up chain over real Whisper+LLM** | **LIVE ✅** | matrix **F-4** (2026-06-14): 75 s clip → Whisper → LLM (`:18783`, claude-haiku-4-5) → posted to chat lane; silent → honest note. Auto-fires on `/record/stop`. |
| **Consent gate + start/stop API live on `:8765`** | **LIVE ⏳** | this runbook, steps 1–5, 7–9 — run it to flip green (no egress worker needed) |
| **Real OGG egress from a live Space** | **GATED** | needs a LiveKit **Egress worker** + shared **Redis** alongside the SFU; not deployed on this box. `/record/start`'s gate passes, but `Recorder.start()` → LiveKit egress has no worker to service it. This is the one true gap for "recorded a Space live." |
| Replay UI (serve the OGG back) | **LIVE ⏳ / not built** | recordings land in `~/.skchat/spaces-recordings/`; a dedicated replay UI is tracked separately (write-up-to-chat is the shipped consumer path) |

**Summary:** Everything except the **actual OGG file produced by a live SFU
egress** is either CI-proven or already LIVE (the write-up chain, F-4). Running
this runbook flips the **consent gate + start/stop API + write-up** to LIVE ✅
without any new infra. The remaining leg — a real recorded OGG — is **GATED on
deploying a LiveKit Egress worker** (Redis-coordinated) next to the SFU at
`noroc2027.tail204f0c.ts.net:8443`. None of the wave-5 federation code is involved;
this is purely an Egress-worker deployment.
