# ▶ Sovereign Conf Calls — Master Plan (2026-06-18)

Send a JOIN LINK (Tailscale **or** public internet) → recipient joins a multi-party
**video + screenshare** conference from a browser. Joiner chooses identity: **SOVEREIGN**
(capauth, Chef's real identity) or **GUEST** (anonymous, unauthenticated random user).
Chef can **pull in the AI agent** (Lumina). **Standalone single instance FIRST, then
federation.** Designed by a 4-agent planning swarm. Coord epic `3ceb6da1` (tag `conf-calls`).

## The big finding: most of this is already built
- **LiveKit SFU v1.9.1 LIVE** on .158 (`livekit-server.service`, tailnet-bound `100.108.59.57:7880`, keys `skchat-opus`/`skchat-lumina`, **no STUN/TURN**, `use_external_ip:false`).
- **Web client done**: `static/livekit.html` (2427 lines) already has a video grid + **getDisplayMedia screenshare** publish + data lanes.
- **Guest invites done but UNWIRED**: `guest.py` has signed room-scoped invite issue/verify/revoke, but `register_guest_routes(app)` is **never called in webui.py** → `/guest/*` all 404. ← the #1 unblocker.
- **Spaces + federation shipped on `main`**: `spaces/` (audio rooms, roles **audio-only**), `sk-lk-authd` = `spaces/federation/authd.py` `/sfu/get` (capauth-signed cross-realm mint), Nostr discovery `/sfu/candidates` + `skchat-nostr-relay.service`. Never run as 2 instances.
- **Agent-in-call**: `lumina-creative/scripts/lumina-call.py` joins ONE room at startup (`--room`), audio (kokoro/piper) + static-portrait video path; MuseTalk avatar OFF (latency).

## The real gaps
1. Guest routes unwired (the public flow's gate).
2. No **video** conf surface — Spaces roles are audio-only; need a `conf/` module with camera+screenshare grants.
3. **coturn not deployed** — no ingress carries WebRTC UDP media; public NAT'd guests need TURN. THE hard infra gap.
4. No "pull in my agent" runtime action (agent is single-room-at-startup).
5. `/livekit/token` is an open impersonation hole (any caller picks any identity) — must be gated before public exposure.

## Ingress decision (per Chef): **Tailscale Funnel**
Funnel is the public ingress for join pages + wss **signaling** (NOT Caddy/CF — the CF token is DNS-only anyway). **Caveat:** Funnel is HTTP(S)-only — it cannot carry WebRTC **UDP media**, so the media path still needs **coturn** (or LiveKit public-UDP / ICE-over-TCP — flagged for investigation in the INGRESS task). Tailnet joiners use the existing direct path, no TURN.

## Epics & tasks (coord board, tag `conf-calls`)

### Phase 1 — STANDALONE (ship v1)
**Identity / plumbing**
- `9edc0b1e` **Wire guest join routes** (critical, no deps) ← start here
- `a517c60e` Conf video role→grant tier (camera+mic+screenshare)
- `d07c6911` Sovereign local-join endpoint (capauth-proven `/join/sovereign`) ← dep roles
- `11f3aec0` Gate open `/livekit/token` + Lumina proven-FQID agent token
- `31dae903` Unified join link + chooser page (Sovereign vs Guest)
- `ff744786` Guest hardening (persistent revoke, anti-spoof badge, single-use)
- `b25d1f74` Waiting-room / admit flow (public guests require admit)

**Conference room core**
- `7bc0d8b1` Conf room model + lifecycle (video, distinct from audio Spaces)
- `4d7f6247` Conf REST routes (create/token/participants/end/list)
- `922c1460` Conf web client (fork livekit.html → conf.html)

**Network exposure**
- `d5b00d43` **Deploy coturn** + ICE tier-3 (critical — the public-media gap)
- `df42e2a4` Public-aware SFU endpoint selection (public wss vs tailnet)
- `707e28ac` **Public ingress via Tailscale Funnel** (signaling + pages)

**Agent**
- `34bd409b` Pull-in-my-agent (Lumina joins arbitrary conf room on request)

**Ops & verify**
- `9addb711` Standalone ops (health, metrics, agent-worker supervision)
- `9ee6a390` E2E public conference verification + runbook ← **ships v1**

### Phase 2 — FEDERATION
- `5b80a6d0` Stand up 2nd sovereign instance (testbed) on .41/.100
- `1afbfb1a` Cross-instance conf join via sk-lk-authd
- `64a50eec` Federated agent + federation observability

## Standalone↔federation seam (clean cut)
- **Standalone** = local `/livekit/token` + new `/conf/*` (local SFU secret/trust, one instance).
- **Federation** = `/sfu/get` → `authd.authorize()` (capauth-signed cross-realm assertion + TrustPolicy + Nostr discovery) — strictly additive; the SFU does zero identity logic either way. Conf routes must keep the token-mint seam identical to Spaces so `sk-lk-authd` wraps it later.

## Suggested first move
`9edc0b1e` (wire guest routes) ∥ `d5b00d43` (coturn — long pole, start now) ∥ `7bc0d8b1`+`a517c60e` (conf model + roles). Then `4d7f6247` (conf routes) freezes the API and unblocks UI/agent/identity in parallel.
