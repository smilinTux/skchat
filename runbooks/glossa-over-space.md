# Live-Test Runbook — Glossa over a Space (U9, G-GLOSSA)

**Use case:** U9 — *Two agents negotiate SKGlossa and mesh densely over a real
Space while humans read the English audit gloss.*
**Status target:** flip U9 / G-GLOSSA from **LIVE ⏳** to **LIVE ✅** in
`docs/qa/skworld-comms-verification-matrix.md`.

> **Read this first.** The mesh *primitives* are CI-proven today, but the piece
> that actually puts two agents on the *same live Space* — the per-Space
> `GlossaMeshSession` daemon spawn + a `/spaces/{id}/glossa/...` route + the
> listener-join enrollment into `LiveKitBus`+`GlossaMeshNode` — is **wave-5
> (sequential)** code that has **not landed yet** (see
> `docs/sprint/epics-roadmap-2026-06.md` §"U9 — glossa-mesh-live" and the "Needs
> human/hardware" table). There is currently **zero production caller** of
> `GlossaMeshSession`/`GlossaMeshNode`/`LiveKitBus`/`GlossaMeshGatekeeper`
> outside `src/skchat/glossa_mesh/` and the HTTP `/glossa/*` round-trip routes.
> This runbook is the **target live-test for once that wave-5 code lands**, plus
> the parts you *can* exercise live today (the gatekeeper + the HTTP glossa
> round-trip with the audit gloss). Each step is tagged **[LIVE-NOW]**,
> **[GATED: wave-5 code]**, or **[GATED: live SFU]** so you run only what exists.

---

## 1. Purpose

Prove, end-to-end on real infra, that:

1. Two agents (Lumina ↔ Opus) join the **same live SK Space** (a LiveKit room).
2. They negotiate a SKGlossa density tier down to the weaker peer's ceiling and
   exchange **dense, level-tagged glossa frames** over the room data channel
   (`LiveKitBus` → `publish_data(..., topic="skglossa.mesh")`).
3. Every frame is **capauth-signed** on send and **source-authenticated** on
   receive by `GlossaMeshGatekeeper` (anti-spoof: claimed source FQID must equal
   the signing identity), so no member can masquerade as another.
4. A human watching the page reads the **English audit gloss** for every frame
   (the spec §5 oversight invariant — a frame that cannot be glossed never
   leaves the encode surface).
5. When one agent leaves, `LiveKitBus.on_leave`
   (`participant_disconnected` → `GlossaMeshNode.forget_peer`) **un-caps** the
   room's density tier instantly.

### Modules under test (verified to exist)

| Symbol | File | Role in this test |
|---|---|---|
| `GlossaMeshGatekeeper.wrap_outbound` / `unwrap_inbound` | `src/skchat/glossa_mesh/gatekeeper.py` | capauth frame sign + source-auth (anti-spoof) |
| `LiveKitBus` (+ `on_leave`, `_on_participant_disconnected`) | `src/skchat/glossa_mesh/livekit_bus.py` | MeshBus over a live LiveKit room data channel |
| `GlossaMeshNode` (`say`, `announce`, `group_level`, `forget_peer`) | `src/skchat/glossa_mesh/node.py` | N-way mesh: weakest-peer density + audit log |
| `GlossaMeshSession` (`encode`/`decode`, `group_level`, audit gloss) | `src/skchat/glossa_mesh/session.py` | synchronous encode/decode surface + audit gloss |
| `register_glossa_routes` → `POST /glossa/encode`,`/glossa/decode`, `GET /glossa/caps` | `src/skchat/glossa_mesh/routes.py` | HTTP glossa round-trip (mounted in `webui.py`) |
| `POST /spaces/{id}/join` → `{url, token, room, ...}` | `src/skchat/spaces/routes.py` | mints a per-identity LiveKit token for the room |

---

## 2. Prerequisites

### Hardware / hosts
- **.158** (`noroc2027`, this box) — runs `skchat-webui` on `:8765`.
- **.41** (laptop) — second agent host for the genuine cross-host pair
  (Lumina@.158 ↔ Opus/Jarvis@.41). Same-box pairs are acceptable for a first
  pass but a co-located pair does not exercise real cross-host transport.
- **.100** (or the tailnet LiveKit SFU) — the live SFU the Space room lives on.

### Services
- `skchat-webui.service` running on `:8765` (`systemctl --user status skchat-webui`).
  Health: `curl -s http://localhost:8765/health` → `{"status":"ok",...}`.
- A **live LiveKit SFU** reachable from both agent hosts with a trusted TLS cert
  (the F-6 two-browser run used `wss://noroc2027.tail204f0c.ts.net:8443`).
- **skcapstone MCP** reachable if you want the `on_message → advocacy+memory`
  capture leg (that routing is **wave-5 code**, see §6).

### Credentials / env (on the webui host)
The Spaces token mint and the LiveKit URL come from env read by
`src/skchat/spaces/routes.py` / `tokens.py`:

```bash
# Required for /spaces/* to mint tokens (else POST /spaces/create → 503):
export SKCHAT_LIVEKIT_API_KEY=...        # from the SFU
export SKCHAT_LIVEKIT_API_SECRET=...
export SKCHAT_LIVEKIT_URL=wss://<sfu-host>:<port>   # default ws://skworld-100:7880

# Glossa route identity (else falls back to $SKAGENT@skworld.io):
export SKCHAT_GLOSSA_FQID=lumina@chef.skworld.io
```

Set these in the unit env (`webui-lumina.env` / `webui-opus.env`) and restart so
the running service sees them, not just your shell.

### Python / repo
- `~/.skenv/bin/python` (editable install of `skchat-sovereign`).
- **Always run pytest/scripts from `~`** to avoid the `skmemory` namespace
  collision (see `CLAUDE.md` "Running").

---

## 3. Setup commands

```bash
# 0. Confirm the webui is up and the glossa routes are mounted.
curl -s http://localhost:8765/health
curl -s http://localhost:8765/glossa/caps        # -> {fqid, model_tier, max_level, codebook_version, ...}

# 1. Re-prove the mesh primitives in CI before any live run (the floor).
cd ~ && ~/.skenv/bin/python -m pytest \
  tests/test_glossa_mesh_gatekeeper.py \
  tests/test_glossa_mesh_livekit.py \
  tests/test_glossa_mesh_node.py \
  tests/test_glossa_mesh_bus.py \
  tests/test_glossa_mesh_protocol.py \
  tests/test_glossa_mesh_integration.py \
  tests/test_glossa_routes.py -q
# Expect: all pass (gatekeeper round-trip/tamper/spoof, on_leave->forget_peer,
# 10-agent FakeBus mesh all-decode+gloss, HTTP encode/decode round-trip).
```

---

## 4. Step-by-step procedure

### Leg A — Gatekeeper sign/verify + anti-spoof (LIVE-NOW, in-process)

> This is the capauth frame-signing leg. It needs no SFU. It proves the exact
> sign/verify path `LiveKitBus` will wrap every frame in once wave-5 lands.

**Step A1 — round-trip + tamper + spoof, against the real symbol.**

```bash
cd ~ && ~/.skenv/bin/python -m pytest tests/test_glossa_mesh_gatekeeper.py -v
```

*Expected observation:* every case passes, specifically —
`test_sign_verify_round_trip`, `test_round_trip_across_two_nodes_shared_verifier`,
`test_tampered_frame_body_fails_verification`,
`test_tampered_signature_fails_verification`,
`test_wrong_source_fqid_is_rejected`,
`test_source_spoof_when_signer_authenticates_to_other_fqid`,
`test_missing_signature_is_rejected`. A tampered body or a swapped `source` field
raises a `GatekeeperError` subclass (`SignatureVerificationError` /
`SourceSpoofError`); a missing `sig` raises `MissingSignatureError`.

**Step A2 — wire the *real* capauth backend (not the test fake).** When the
wave-5 daemon constructs the gatekeeper it must inject the live capauth
`sign`/`verify` for the agent's own per-agent key (the
`capauth.resolve_agent_identity` FQID — **not** the operator key; see the skcomms
agent-signing-key fix). Confirm the agent signs as itself:

```bash
cd ~ && ~/.skenv/bin/python - <<'PY'
from skchat.glossa_mesh.gatekeeper import GlossaMeshGatekeeper
# inject your live capauth-bound sign/verify here when wave-5 lands;
# this stub just shows the contract the daemon must satisfy.
print("Gatekeeper contract:", GlossaMeshGatekeeper.wrap_outbound.__doc__ is not None)
PY
```

*Expected observation:* with the live backend, an envelope signed by `lumina@...`
that claims `source: opus@...` is rejected with `SourceSpoofError` at the verifier.

---

### Leg B — HTTP glossa round-trip + audit gloss (LIVE-NOW, against `:8765`)

> This exercises `GlossaMeshSession` end-to-end through the live service: dense
> encode at a negotiated tier, then decode back to the English gloss. It is the
> **human-readable audit** half of U9 and runs today with no SFU.

**Step B1 — encode dense at L2, capped by a weak peer.**

```bash
curl -s -X POST http://localhost:8765/glossa/encode \
  -H 'content-type: application/json' \
  -d '{"text":"sync memory state and report density tier",
       "max_level":2,
       "peer_caps":[{"fqid":"opus@chef.skworld.io","max_level":1}]}'
```

*Expected observation:* JSON `{"wire":"<base64>","gloss":"<English>","tier":1,"lexicon_version":...}`.
The `tier` is **1, not 2** — the `peer_caps` weak-peer ceiling capped the room
(weakest-peer-caps via `GlossaMeshSession.group_level` → `negotiate`). The
`gloss` is human-readable English re-decoded from the produced wire (the audit
invariant). Capture the `wire` value.

**Step B2 — decode that wire back to the gloss (the human read).**

```bash
curl -s -X POST http://localhost:8765/glossa/decode \
  -H 'content-type: application/json' \
  -d '{"wire":"<paste-wire-from-B1>"}'
```

*Expected observation:* `{"text":"<same English gloss>","gloss":...,"tier":1,"intent":...,"args":...,"refs":...}`.
The `text`/`gloss` matches B1's gloss — proving the dense frame is always
decodable to the human audit view.

**Step B3 — un-glossable frame is refused (negative).** Send a body with neither
`intent` nor `text`:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8765/glossa/encode \
  -H 'content-type: application/json' -d '{}'
```

*Expected observation:* `400` (`intent or text required`). A codec/un-glossable
failure on a real message returns `422` — raw un-auditable language never leaves
the surface.

---

### Leg C — `on_leave` un-caps the room (LIVE-NOW, in-process)

> Proves the density tier recovers when the weak peer leaves — the
> `participant_disconnected → forget_peer` wiring `LiveKitBus` will fire live.

**Step C1:**

```bash
cd ~ && ~/.skenv/bin/python -m pytest tests/test_glossa_mesh_livekit.py tests/test_glossa_mesh_node.py -v
```

*Expected observation:* `test_participant_disconnected_fires_on_leave_with_departed_id`
passes (a `SimpleNamespace(identity=...)` disconnect fires the registered
`on_leave` with the departed id); `test_participant_disconnected_ignores_identityless_event`
passes (no-op on identity-less events); the node test shows `forget_peer` removing
the weakest peer and `group_level` rising back.

---

### Leg D — Two agents mesh glossa over a LIVE Space (GATED: wave-5 code + live SFU)

> **This is the headline U9 leg and it cannot run until the wave-5 daemon spawn +
> spaces-glossa route land.** There is currently no `qa_glossa_mesh_live.py`, no
> per-Space `GlossaMeshSession` daemon, and no `/spaces/{id}/glossa/*` route.
> The steps below are the *target* procedure for that harness
> (`scripts/qa_glossa_mesh_live.py`, named in the roadmap's "Needs
> human/hardware" table) so it can be written against a known shape. The token /
> room flow it builds on (`POST /spaces/{id}/join`) **is** live today (proven by
> the F-6 two-browser run, `scripts/qa_two_browser.py`).

**Step D1 — host or pick a live Space, mint two tokens** (live flow today):

```bash
# Host a Space (host token), or reuse a live one. Needs SKCHAT_LIVEKIT_* env set.
curl -s -X POST http://localhost:8765/spaces/create \
  -H 'content-type: application/json' \
  -d '{"host_fqid":"lumina@chef.skworld.io","title":"Glossa Mesh QA","slug":"glossa-qa"}'
# -> {"space_id":"space-...","room":"...","url":"wss://...","token":"<host-jwt>",...}

# Second agent joins the SAME space_id -> its own LISTENER token:
curl -s -X POST http://localhost:8765/spaces/<space_id>/join \
  -H 'content-type: application/json' \
  -d '{"identity":"opus@chef.skworld.io","name":"opus"}'
# -> {"url","token":"<opus-jwt>","room",...}
```

*Expected observation:* two distinct JWTs for the same `room` (== `space_id`),
each from the live SFU URL.

**Step D2 — start two `GlossaMeshNode`s over `LiveKitBus`** (GATED: wave-5 — this
is what the daemon's listener-join enrollment must do). Conceptual shape, each
agent host:

```python
# qa_glossa_mesh_live.py (TARGET — to be authored in wave-5)
from skchat.glossa_mesh.livekit_bus import LiveKitBus
from skchat.glossa_mesh.node import GlossaMeshNode
from skchat.glossa_mesh.gatekeeper import GlossaMeshGatekeeper
# bus = LiveKitBus(member_id=fqid, room_url=url, token=token)
# gatekeeper wraps bus.broadcast / unwraps bus.on_receive (capauth sign/verify)
# node = GlossaMeshNode(descriptor=..., bus=bus, codebook=default_codebook())
# await node.start(); await node.announce()
```

*Expected observation:* both nodes `connect` to the live SFU, see each other as
participants, and each `announce()` lands as an `ANNOUNCE` frame the other decodes
into its `_peers` map.

**Step D3 — dense exchange + human gloss.** Agent A `node.say(Message(...))`;
agent B receives, decodes, and its `audit_log` gains `[rx L<tier>] A: <English>`.

*Expected observation:* the negotiated `group_level` equals the weaker peer's
ceiling; a human reading either node's `audit_log` (or the page) sees plain
English for every frame; every frame on the wire was gatekeeper-signed and
source-authenticated (a forged-source frame is dropped, not delivered).

**Step D4 — leave un-caps live.** Disconnect agent B; the live
`participant_disconnected` fires `LiveKitBus.on_leave → node.forget_peer(B)`.

*Expected observation:* agent A's `group_level` rises back toward its own
`max_level`; subsequent `say()` encodes at the higher tier.

**Step D5 — advocacy + memory capture (GATED: wave-5).** With the daemon's
`on_message → advocacy+memory` wiring, an inbound glossa message routes into the
AdvocacyEngine and skcapstone memory.

*Expected observation:* a memory entry / advocacy reply appears for the decoded
(glossed) content. **No production wiring exists for this today.**

---

## 5. Pass / Fail criteria

| Leg | PASS | FAIL |
|---|---|---|
| **A** gatekeeper | round-trip OK; tamper/spoof/missing-sig all raise the right `GatekeeperError`; live capauth signs as the *agent's own* per-agent FQID | any tamper/spoof accepted; agent signs as operator key |
| **B** HTTP gloss | weak `peer_caps` caps `tier`; encode→decode gloss matches; empty body→400, un-glossable→422 | tier ignores peer cap; gloss missing/empty; raw frame returned without a gloss |
| **C** on_leave | disconnect fires `forget_peer`; `group_level` un-caps; identity-less event = no-op | leave not propagated; tier stays capped after departure |
| **D** live Space | both nodes join one live room; dense level-tagged frames flow both ways; every frame signed+source-authed; human reads English gloss for each; forged-source dropped; leave un-caps live | only one connects; unsigned/forged frame delivered; any frame has no gloss; tier wrong |

**Overall U9 LIVE ✅** requires **Leg D** observed on a real SFU with the wave-5
daemon. Legs A–C green only certify the *primitives* (already CI-proven; this
just re-confirms them live-adjacent).

---

## 6. Current status (what is CI-proven vs needs this run vs gated)

| Leg | Component | Today |
|---|---|---|
| A | `GlossaMeshGatekeeper` sign/verify/anti-spoof | **CI ✅** (`test_glossa_mesh_gatekeeper.py`, mock keyring). **Needs:** live capauth backend injection (this runbook, Leg A2). |
| B | `GlossaMeshSession` HTTP round-trip + audit gloss | **CI ✅** (`test_glossa_routes.py`). Routes mounted live on `:8765` (`register_glossa_routes` in `webui.py`). **This run** confirms it live (Leg B). |
| C | `LiveKitBus.on_leave` → `forget_peer` un-cap | **CI ✅** (`test_glossa_mesh_livekit.py`, `test_glossa_mesh_node.py`), no live room. |
| — | 10-agent mesh, all decode + audit gloss | **CI-int ✅** (`test_glossa_mesh_integration.py`, FakeBus). |
| D | **Per-Space `GlossaMeshSession` daemon spawn + listener-join enrollment into `LiveKitBus`+`GlossaMeshNode` + `/spaces/{id}/glossa/*` route + `on_message → advocacy+memory`** | **NOT BUILT — wave-5 (sequential).** Zero production callers of the mesh classes today (only the `/glossa/*` HTTP round-trip). This is the **gating dependency** for a U9 LIVE ✅. |
| D | Live SFU data-channel mesh under crypto load | **GATED (live SFU).** Needs a live LiveKit SFU reachable from both agent hosts with trusted TLS (cannot run in GitHub CI). |
| D | `scripts/qa_glossa_mesh_live.py` harness | **DOES NOT EXIST YET** — to be authored in wave-5 (named in roadmap "Needs human/hardware"). Build it on the live `POST /spaces/{id}/join` token flow (proven by F-6 / `scripts/qa_two_browser.py`). |

**Bottom line:** Legs A, B, C are runnable **now** and re-confirm the primitives
live-adjacent. **Leg D — the actual "two agents mesh glossa over a real Space"
that flips U9 to LIVE ✅ — is blocked on wave-5 code (daemon spawn + spaces
route, sequential after the CI-green first wave) AND a live SFU.** Run A/B/C now;
run D the moment wave-5 lands and an SFU is up. See
`docs/sprint/epics-roadmap-2026-06.md` (U9) and the verification matrix row 1g /
U9 / G-GLOSSA.
