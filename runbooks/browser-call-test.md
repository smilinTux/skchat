# Browser Call Test — 1:1 audio/video between two browsers (U5 / G-CALL)

**Use case U5** (verification matrix §3): *place a 1:1 audio/video call between two
browsers.* This runbook drives the sovereign call path end-to-end: pair → ring
(signed `CALL_INVITE`) → answer → both browsers land in the same deterministic
LiveKit room and exchange real A/V.

**Status in the matrix:** G-CALL is **CI-int → LIVE ⏳**. The wiring (routes,
session, connectivity, sig-gate) is CI-green; this runbook is the live re-verify
that flips U5 to **LIVE ✅**. See "Current status" at the bottom for the per-leg
breakdown.

---

## Purpose

Prove, on real infra, that two browsers can complete a 1:1 call through the
sovereign path:

- the **ring** is a capauth-signed `CALL_INVITE` delivered over skcomms (not a
  bare LiveKit invite);
- `/call/incoming` only surfaces invites that are **signature-valid** *and*
  **addressed to self** (the pairing sig-gate);
- both peers derive the **same** room with zero negotiation
  (`derive_room()` → `call-<16 base32>`), mint their own FQID-identity token, and
  join — exchanging audio (and video if cameras enabled).

---

## Prerequisites

### Services (on .158 / `noroc2027`)
- **`skchat-webui@lumina`** running on **`:8765`** (`systemctl --user status skchat-webui@lumina`).
  Env in `~/.config/skchat/webui-lumina.env`.
- **LiveKit SFU** reachable at **`wss://noroc2027.tail204f0c.ts.net:8443`**
  (`SKCHAT_LIVEKIT_URL` in the webui env). The webui only mints JWTs — it does
  **not** host the SFU.
- LiveKit creds present in the webui env: `SKCHAT_LIVEKIT_API_KEY=skchat-lumina`
  + `SKCHAT_LIVEKIT_API_SECRET=…` (without these, `/call/start` returns **503
    "livekit not configured"** — that is `_have_creds()` failing).
- **`livekit-api`** installed in `~/.skenv` (soft dep for `_mint_token`; missing
  → 503 "livekit-api not installed").
- The two identities must be **paired** in skcomms (peer dir
  `~/.skcapstone/peers/`). `/call/start` 404s "peer not paired" otherwise.

### Hardware / client
- Two browsers on the **tailnet** (e.g. phone A + laptop B, or two desktop
  profiles), both signed into Tailscale. Tier-1 ICE assumes both peers
  `on_tailnet` (see `connectivity.py`); off-tailnet needs coturn env
  (`SKCHAT_TURN_SECRET`/`SKCHAT_TURN_URLS`), which is **not** wired per-peer yet.
- A microphone on each device (camera optional). `tailscale serve` over HTTPS is
  a **secure context**, so `getUserMedia` (mic/cam) is permitted.

### Optional (agent-as-callee instead of a second human)
- **`skchat-lumina-call.service`** — Lumina's conversational LiveKit agent.
  Needs **.100 GPU** services up (TTS `:15091` / narrate `:8082`).
  Start only if you want Lumina (not a second browser) on the far end.

---

## Setup commands

```bash
# 1. Confirm the webui is live and LiveKit is configured server-side.
curl -s http://localhost:8765/livekit/config | python3 -m json.tool
#   expect: {"url":"wss://noroc2027.tail204f0c.ts.net:8443", ...,"available":true}
#   if "available":false → API key/secret missing in webui-lumina.env (fix + restart).

# 2. Confirm the call routes are mounted and paired peers exist.
curl -s http://localhost:8765/call/peers | python3 -m json.tool
#   expect: {"peers":[{"fqid":"chef@…","fingerprint":"…"}, …]}
#   empty list → nothing paired; pair first at /pair (skcomms QR/TOFU).

# 3. (optional) Restart the webui to pick up any env edit.
systemctl --user restart skchat-webui@lumina
```

The webui must be reached over the tailnet HTTPS host (secure context) from the
browsers: **`https://noroc2027.tail204f0c.ts.net`** (Tailscale serve fronts
`:8765`). `http://localhost:8765` is for the server-side curls above only.

---

## Step-by-step procedure (two humans, two browsers)

> Browser **A** = caller (e.g. `chef`), Browser **B** = callee (e.g. another
> paired identity / second profile). Both open the **pair page**, which carries
> the 📞 Call button and the incoming-call ring banner.

### Step 1 — Open the pair page in both browsers
**Command (each browser):** navigate to
`https://noroc2027.tail204f0c.ts.net/pair`
**Expected:** the "Paired peers" list renders (one `<button>📞 Call</button>` per
peer, from `GET /call/peers`). A hidden `#ring-banner` sits at the top. B's page
begins polling `GET /call/incoming` every **4 s** (`pollRing`).

### Step 2 — A rings B
**Command (browser A):** click **📞 Call** next to B's FQID.
This fires `POST /call/start {"peer":"<B-fqid>"}`.
**Expected (server side):**
- `_prepare_call` resolves the peer, derives the room via `derive_room(A,B)` →
  `call-<16 lowercase base32 chars>` (order-independent — both sides compute the
  same name), mints A's token (`identity = A's FQID`, TTL 6 h).
- `_send_invite` sends a **`CALL_INVITE`** envelope to B over
  `skcomms.mailbox.send_message` (subject `CALL_INVITE`, signed).
- Response JSON: `{room, token, livekit_url, peer_fqid, identity}`.
**Expected (browser A):** redirects to
`/livekit?room=<call-…>&identity=<A-fqid>&token=<JWT>` and **auto-connects**
(the page auto-joins when `room` + `identity` query params are present, and uses
the pre-authorized `token` from the call start instead of re-minting). Status pill
→ `connected`; A appears in the room.

**Verify the ring on the wire (optional, server side):**
```bash
# As B's identity, list signature-valid invites addressed to B, newest first.
curl -s http://localhost:8765/call/incoming | python3 -m json.tool
#   expect: {"invites":[{"type":"CALL_INVITE","from_fqid":"<A>","to_fqid":"<B>",
#            "room":"call-…","transport":"livekit","livekit_url":"wss://…","ts":…,"nonce":"…"}]}
```

### Step 3 — B sees the ring banner
**Expected (browser B, within ~4 s):** the green `#ring-banner` shows
`📞 Incoming call from <A-fqid>` + an **Accept** button. This is driven by
`pollRing()` → `GET /call/incoming`, which is the **sig-gate**: only invites
whose envelope is signature-`valid` **and** whose `to_fqid == self` are surfaced.
An unsigned, tampered, or mis-addressed invite never appears here.

### Step 4 — B answers
**Command (browser B):** click **Accept**.
This fires `POST /call/answer {"peer":"<A-fqid>"}`.
**Expected (server side):** `_prepare_call` re-derives the **same** room
`call-<…>` and mints B's own token — but **answering never rings** (no
`_send_invite`). Response `{room, token, livekit_url, peer_fqid, identity}`.
**Expected (browser B):** redirects to
`/livekit?room=<same call-…>&identity=<B-fqid>&token=<JWT>` and auto-connects.

### Step 5 — Allow mic (and camera) in both browsers
**Expected:** each browser prompts for mic/cam; on allow, the local track
publishes (`RoomEvent.LocalTrackPublished` logged) and a tile renders.

### Step 6 — Confirm two-way media
**Expected (both browsers):** each shows **2 participants** (self + peer); audio
flows both ways (speak in A → heard in B and vice-versa); if cameras allowed,
video tiles render. The page log shows
`subscribed audio … from <peer-identity>` on each side.

**Authoritative server-side check (who's actually in the room):**
```bash
~/.skenv/bin/python - <<'PY'
import asyncio, os
os.environ.update(
    LIVEKIT_URL='wss://noroc2027.tail204f0c.ts.net:8443',
    LIVEKIT_API_KEY='skchat-lumina',
    # secret: see SKCHAT_LIVEKIT_API_SECRET in ~/.config/skchat/webui-lumina.env
    LIVEKIT_API_SECRET=os.environ.get('SKCHAT_LIVEKIT_API_SECRET',''))
from livekit import api
async def m():
    lk = api.LiveKitAPI()
    for r in (await lk.room.list_rooms(api.ListRoomsRequest())).rooms:
        print(r.name, r.num_participants)
        for p in (await lk.room.list_participants(api.ListParticipantsRequest(room=r.name))).participants:
            print('  ', p.identity, p.state, [t.type for t in p.tracks])
    await lk.aclose()
asyncio.run(m())
PY
```
**Expected:** the `call-<…>` room lists **2 participants** (A's FQID + B's FQID),
each `ACTIVE` with at least an `AUDIO` track.

### Step 7 — Check the ICE tier (optional)
```bash
curl -s 'http://localhost:8765/connectivity/ice?peer=<B-fqid>' | python3 -m json.tool
#   expect: {"ice_servers":[],"policy":"all","preferred_tier":1,"on_tailnet":true}
```
Tier 1 (tailnet, no relay) is the default deployment. The browser fetches this
same endpoint and applies `iceServers`/`iceTransportPolicy` before connecting.

---

## Variant — quick two-human test without the pair UI

If you just want a plain two-way call (no ring/sig-gate), open the LiveKit page
directly in each browser with the **same room** but **different identity**, and
let each page mint its own token:

```
# Browser A
https://noroc2027.tail204f0c.ts.net/livekit?room=manual-test&identity=chef
# Browser B
https://noroc2027.tail204f0c.ts.net/livekit?room=manual-test&identity=guest
```
Auto-connect fires because `room` + `identity` are present; with no `token` param
each page mints via `POST /livekit/token`. This bypasses pairing/`CALL_INVITE` —
it tests media + SFU only, not the sovereign ring path.

---

## Variant — automated headless verification (data-channel leg)

The real-time peer leg already has a Playwright harness (matrix finding **F-6**):

```bash
~/.skenv/bin/python scripts/qa_two_browser.py
#   launches two headless Chromium contexts into the SAME Space room,
#   each with its own Space-minted token (POST /spaces/{id}/join),
#   A publishLane(chat) over the WebRTC data channel → B receives via
#   RoomEvent.DataReceived. exit 0 = PASS.
```
This proves two browsers connect to the live SFU and round-trip a data-channel
message. It does **not** exercise `/call/start`'s `CALL_INVITE` ring path — that
is the human leg this runbook covers. (Cannot run in GitHub CI: needs the live
webui + reachable SFU with trusted TLS + a full Chromium build.)

---

## Pass / Fail criteria

**PASS** (all must hold):
1. `GET /livekit/config` → `"available":true`.
2. `POST /call/start` (A→B) returns `{room:"call-…", token, livekit_url, peer_fqid, identity}` (HTTP 200).
3. `GET /call/incoming` as B shows the invite with `type:"CALL_INVITE"`,
   `to_fqid == B`, valid signature (sig-gate passes).
4. A and B derive the **identical** `call-<…>` room (Step 2 + Step 4 responses match).
5. Server-side room listing shows **2 participants**, each with an `AUDIO` track.
6. Audio is audible **both** directions (and video renders if cameras allowed).
7. `B answers without re-ringing` — no second `CALL_INVITE` is generated by `/call/answer`.

**FAIL** indicators + likely cause:
- `503 livekit not configured` on `/call/start` → API key/secret missing →
  fix `webui-lumina.env`, restart.
- `404 peer not paired` → identities not in `~/.skcapstone/peers/` → pair at `/pair`.
- `409 ambiguous bare name` → use the full FQID, not the bare handle.
- Ring banner never appears on B → invite signature invalid or `to_fqid` mismatch
  (sig-gate correctly dropping it) **or** B's page not polling (check `/call/incoming`
  directly).
- Both join but no audio → ICE/SFU reachability (confirm `:8443` reachable from the
  browser's tailnet; check `preferred_tier`); off-tailnet needs coturn env (not wired).
- Page loads but never connects → check browser console for the livekit-client ESM
  load (the F-6 fix removed a fatal bad `TrackPublishOptions` import; a regression
  there breaks `room`/`connect`).

---

## Current status

| Leg | Verification |
|---|---|
| Call routes (`/call/start` ring, `/call/answer`, `/call/incoming` sig-gate, `/call/peers`) | **CI** — `tests/test_call_routes.py` (16 cases) |
| Call session (`derive_room()` per-pair, `CALL_INVITE` build/parse) | **CI** — `tests/test_call_session.py` (12 cases) |
| Connectivity (ICE tier ladder) | **CI** — `tests/test_connectivity.py` (11 cases) |
| Call wiring end-to-end (in-process) | **CI-int** — `test_call_integration.py` |
| Two headless browsers join SFU + data-channel round-trip | **LIVE ✅** — `scripts/qa_two_browser.py` (matrix F-6) |
| **Human 1:1 A/V via the ring/sig-gate path (this runbook)** | **LIVE ⏳** — needs this run on the tailnet with two real browsers |
| Off-tailnet call (Tier-3 coturn relay) | **GATED** — per-peer off-tailnet ICE detection not wired; coturn env not provisioned for calls |
| Agent-as-callee (Lumina answers) | **GATED on .100 GPU** — needs `skchat-lumina-call.service` + TTS/STT/LLM up |

So: the **media + SFU + data-channel** legs are CI-proven and LIVE ✅ (F-6); the
**sovereign ring path** (signed `CALL_INVITE` → sig-gated `/call/incoming` →
`/call/answer` into the shared `call-<…>` room with live two-way human audio) is
CI-int but still **LIVE ⏳** — this runbook is that live verification. Flipping it
to LIVE ✅ (with a server-side 2-participant room listing as evidence) flips **U5**
in the matrix. No wave-5 code is required for the human-browser path; the only
gated extensions are off-tailnet (coturn) and agent-callee (.100 GPU).

---

## Reference (verified symbols)

- `src/skchat/call_routes.py` — `register_call_routes`, `_prepare_call`,
  `_send_invite`, `_resolve_peer`; routes `/call/start` `/call/answer`
  `/call/incoming` `/call/peers` `/connectivity/ice`.
- `src/skchat/call_session.py` — `derive_room()`, `build_invite_body()`,
  `parse_invite_body()`, `CALL_INVITE_SUBJECT`.
- `src/skchat/connectivity.py` — `ice_config()` (Tier 1 tailnet → Tier 3 coturn).
- `src/skchat/livekit_routes.py` — `register_livekit_routes`, `_mint_token()`,
  `/livekit/config`, `/livekit/token`, `/livekit` page.
- `src/skchat/static/livekit.html` — `connect()`, auto-join on `room`+`identity`,
  pre-authorized `token` reuse, `window.__skRoom`/`__skPublishLane` test hooks.
- `src/skchat/webui.py` — `/pair` page (`_PAIR_HTML`): 📞 Call button
  (`callPeer`→`/call/start`), ring banner (`pollRing`→`/call/incoming`, 4 s),
  Accept (`answerPeer`→`/call/answer`).
- Harness: `scripts/qa_two_browser.py`.
