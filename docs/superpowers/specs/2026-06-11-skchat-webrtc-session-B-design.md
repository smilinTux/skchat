# Spec: WebRTC Session — Sub-project B (Sovereign P2P peer link: data + media)

**Date:** 2026-06-11
**Coord task:** `7f28ac51` (P2P phase) — follows sub-project A (merged, skchat PR #4)
**Status:** Design — pending approval
**Doctrine:** *"If you need one, get two."* Redundancy at every layer — dual signaling,
dual media tracks (graceful-degrade video), ICE tier ladder, and B itself is the P2P
half of C's P2P-or-SFU pair.
**Primary repo:** `skcomms` (the P2P transport); integration + UI in `skchat`.

## Goal
Two paired peers (agent↔agent or agent↔browser) establish a **direct WebRTC
PeerConnection** — a reliable **data channel** plus **audio** (and graceful-degrade
**video**) — with **no SFU**. Signaling rides our verified channels; NAT traversal uses
the connectivity ladder; every SDP is capauth-signed and verified (same sovereign-trust
guarantee as the A-phase CALL_INVITE ring, which today's signing fix made real).

## Context — this is mostly "finish + wire + harden," not greenfield
skcomms already has an aiortc P2P stack (mapped 2026-06-11):
- **DONE:** `RTCPeerConnection` setup; ordered/reliable **data channel** (`skcomms` label)
  agent↔agent; WebSocket signaling **broker** (`/webrtc/ws`) with CapAuth auth +
  anti-spoof; STUN/TURN config incl. HMAC time-limited creds; `TTSAudioTrack` +
  `MuseTalkVideoTrack`; browser↔agent media **receive** path (`facetime.html`).
- **GAPS (this spec):** `aiortc` not installed; outgoing SDP **unsigned** (verify exists,
  sign is a TODO); agent↔agent is **data-only** (media tracks not attached); the new
  `skchat/connectivity.py` tier ladder is **not consumed**; agent↔agent media pipeline
  undefined; browser **mic→agent** missing.

## Non-goals (deferred)
- **C:** the P2P→LiveKit layered fallback + Talk-compat shim (B delivers the P2P half C
  falls back *from*; the deterministic room from A is the shared landing zone).
- Group P2P (>2 peers / mesh).
- MuseTalk video *quality* work — B wires the track + graceful degrade, not avatar tuning.

## Architecture — units

### Unit 1 — `aiortc` install (step 0)
Add/confirm the `skcomms[webrtc]` extra and install into `~/.skenv`. All transport code
already imports `aiortc` under a soft-dep guard; install makes the path live. Verify
`import aiortc` + the existing webrtc tests run.

### Unit 2 — Sign outgoing SDP (skcomms `webrtc.py` + `webrtc_media.py`)
Wrap outgoing offers/answers in the capauth signature envelope that the incoming path
already verifies (`webrtc.py` ~617-659). Reuse the **same EnvelopeSigner / capauth key
resolution fixed today** (per-agent `capauth/identity`, not the operator key). A peer
that can't verify the SDP signature is rejected before `setRemoteDescription`. Closes the
MITM gap on the media-negotiation path.

### Unit 3 — Dual signaling (`SignalingChannel` interface, two backends)
A small interface both backends implement: `send_signal(peer, kind, payload)` +
an inbound callback. `kind ∈ {offer, answer, ice}`.
- **Mailbox backend (sovereign default):** SDP/ICE as **signed skcomms envelopes** with
  subjects `CALL_SDP_OFFER` / `CALL_SDP_ANSWER` / `CALL_ICE` — identical mechanism to the
  A-phase `CALL_INVITE` (build_envelope → sign → peer inbox; `read_inbox` + verify gate).
  Zero signaling server. ICE candidates batch (non-trickle) to tolerate mailbox latency.
- **Broker backend (fast path):** the existing `/webrtc/ws` relay — low-latency trickle
  ICE when both peers can reach it.
- **Selection ("get two"):** mailbox is always available and is the default; the broker is
  used opportunistically when reachable (probe on connect). Negotiation works over either;
  a peer that only has mailbox still completes. Doctrine: never a single point of failure.

### Unit 4 — Connectivity wiring (`ice_config_provider` hook)
`WebRTCTransport` gains an `ice_config_provider(local_fqid, peer_fqid) -> dict` hook.
skchat supplies `connectivity.ice_config(...)` (the Tailscale→LAN→coturn tier ladder),
replacing the hardcoded Google STUN. Tier 1 (both on tailnet) → no ICE servers (direct);
tier 3 → ephemeral coturn creds (the shared `apps/skturn` coturn once live). Default
provider (no skchat) keeps the existing STUN/TURN behavior.

### Unit 5 — Agent↔agent media (data + audio + graceful-degrade video)
Attach to the agent-agent PeerConnection (today data-only):
- **`TTSAudioTrack`** — an agent can **speak P2P** (its TTS audio) to the peer.
- **`pc.ontrack`** handler — receive + consume the peer's audio (feed STT / playback).
- **`MuseTalkVideoTrack`** — attached but **graceful-degrade**: if the avatar/MuseTalk
  GPU pipeline (`.100`) is unavailable, the connection proceeds **audio+data only**
  (no failure). Enabled where the pipeline is up; "get two" without making video a hard dep.
- The **data channel rides alongside** (already works) — the structured agent-to-agent
  link that seeds the agent-native-comms-language north star.

### Unit 6 — Call integration (skchat)
Wire `initiate_call`/`accept_call` (today stubs calling `_schedule_offer`) to drive the
real dual-signaling negotiation, landing the peers in a session keyed by the A-phase
deterministic room id (so C can later fall back to the *same* LiveKit room). `webrtc_status`
reports the live path (mailbox|broker), tier, and tracks.

## Data flow (agent↔agent, mailbox path)
```
opus initiate_call(lumina)
  → ice = ice_config(opus,lumina)                 # tier ladder
  → pc = RTCPeerConnection(ice); pc.createDataChannel; pc.addTrack(TTSAudio)[; video if up]
  → offer = pc.createOffer(); SIGN(offer)
  → SignalingChannel.send(lumina, "offer", signed_offer)   # mailbox default / broker if reachable
lumina ← CALL_SDP_OFFER (verify sig) → setRemote → createAnswer → SIGN → send "answer"
  ↔ ICE exchanged (batched over mailbox / trickle over broker)
  → DTLS/ICE connect → data channel open + audio flowing, P2P, no SFU
```

## Error handling / redundancy
- SDP signature invalid → reject before setRemoteDescription (logged).
- Broker unreachable → mailbox path (no user-visible failure). Mailbox slow → broker if up.
- ICE fails on all tiers → surfaced as `webrtc_status: failed` — this is **C's** trigger
  (fall back to the LiveKit room from A). B reports cleanly; C acts.
- Video pipeline down → audio+data only, connection succeeds.
- `aiortc` absent → transport disabled with a clear hint (existing soft-dep behavior).

## Testing
Unit:
- SDP sign/verify round-trip (reject tampered SDP); uses the real capauth signer.
- Mailbox signaling: `CALL_SDP_OFFER/ANSWER/ICE` envelope build/parse + `to_fqid`/sig gate.
- `ice_config_provider` hook: transport uses provided servers; tier-1 → empty.
- Signaling selection: mailbox-only peer still negotiates; broker chosen when reachable.
Integration (loopback / two in-process transports, aiortc):
- Two PeerConnections negotiate over the **mailbox** backend → data channel opens, a test
  audio track flows, `webrtc_status` shows `path=mailbox`.
- Same over the **broker** backend.
- Video-unavailable path → connection still succeeds audio+data.

## Phasing (each a build slice)
- **B1** — Unit 1 (aiortc) + Unit 2 (SDP signing) + Unit 4 (ICE wiring): harden the
  existing data-channel path with verified SDP + the sovereign tier ladder.
- **B2** — Unit 3: mailbox signaling backend + dual-path selection ("get two").
- **B3** — Unit 5 + Unit 6: agent↔agent audio (+graceful video) + call-integration wiring.

## Files (anticipated)
- skcomms: `transports/webrtc.py`, `transports/webrtc_media.py`, `signaling.py`,
  new `transports/signaling_mailbox.py` (mailbox backend), `pyproject.toml` (extra),
  tests under `skcomms/tests/`.
- skchat: `call_routes.py`/`mcp_server.py` (wire initiate/accept + ice provider),
  `connectivity.py` (expose provider), `static/facetime.html` (mic→agent, later),
  tests under `skchat/tests/`.
