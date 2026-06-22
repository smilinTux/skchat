# ▶ SKChat Unified Sovereign Client + Federation — Master Plan (2026-06-20)

**Goal (Chef):** Merge the two clients into ONE — fold the webui *livechat* feature set into the
**`skchat-app` Flutter app** (full parity). Then **install it as Flutter web per box** and **federate**:
run the **Lumina app on .158** and the **Jarvis app on .41**, and **test video/screenshare calls
back-and-forth**. Federation sequence (Chef): **Shape A first, then Shape B.**

Coord epic: `EPIC: SKChat Unified Client + Federation` — tag `skchat-unify`. Builds on the existing
`cd61fcb2` skchat-unified-comms (batch-G) and the **DONE** `3ceb6da1` Sovereign Conf Calls Phase-1.

---

## Grounded starting state (3-agent scout, 2026-06-20)

**The Flutter app (`~/clawd/skcapstone-repos/skchat-app`) is already substantial:**
- Real 1:1 chat (daemon `POST /api/v1/send` + `skchat` CLI), groups CRUD, peer/QR, identity/capauth/PGP,
  onboarding, notifications, activity, consciousness badge, agent model-picker.
- **Both call tiers work**: P2P WebRTC 1:1 (`webrtc_service.dart`) AND LiveKit SFU group/conf
  *with screenshare* (`livekit_call_service.dart`, `livekit_client ^2.2.6`). Reachable from the
  conversation screen. Spaces (audio rooms) full.
- 6 platform targets incl. **web**; Spaces backend default already `https://noroc2027.tail204f0c.ts.net` (.158).
- batch-G: **G1/G2/G4 landed**, G3/G5 partial, **G6 (cluster control) open**.

**Merge gaps (webui livechat → app):**
1. Spaces/LiveKit/Capstone URLs are **build-time-only** (`String.fromEnvironment`) — no in-app
   instance/host switch (only the daemon URL is runtime-settable). **#1 federation-UX gap.**
2. New **conf REST surface** (`/conf/create`, `/conf/{room}/token`, waiting-room admit/deny,
   invite-agent) is **not consumed** — app builds LiveKit room names internally.
3. **Reactions + typing are UI-only** (model fields + pickers exist; no transport in `skcomms_client`).
4. **No deep-link join** for guest (`/guest/join`) or sovereign (`/join/sovereign`, capauth-signed claim).
5. G6 cluster-control screen, recordings browser, facetime avatar path, watch-together native — all absent.
6. Backend: **`/send` returns HTML** (the one backend gap) — app should use `/inbox` JSON + `/ws/chat` + a JSON send.

**Federation reality:**
- Server-side **cross-realm token mint is built + LIVE** for audio (`/sfu/get`, `spaces/routes.py:382`)
  AND conf (`/conf/{room}/federated-token`, `conf/routes.py:552`) — capauth-signed FQID assertion +
  nonce + `TrustPolicy` + role cap. **But no client ever calls it** → never run E2E.
- Two distinct identities ready: **lumina** ed25519 `@skworld.io`, **jarvis** rsa4096 `@skworld.io`.
  jarvis already **pinned** in `.158:~/.skchat/federation-peers/` and **trusted FULL** in `federation-trust.json`.
- **.41 is partially provisioned**: skchat repo present but **behind** .158 (`500c6d0` vs `af43188`); webui
  runs as **opus** with **test creds**; **no livekit-server on .41**; Nostr relay env empty.
- **Blockers:** Nostr relay on .158 bound `127.0.0.1:7447` (not cross-host); no runtime focus-advertise
  (`publish_focus`/`publish_membership` never called); **FQID realm mismatch** — trust/pins use
  `@chef.skworld`, capauth uses `@skworld.io` (must reconcile so `verify_signed` resolves).

**Two federation shapes (Chef: do A then B):**
- **Shape A — shared SFU on .158:** both apps join .158's LiveKit; jarvis@.41 mints a **cross-realm
  conf token** via `/conf/{room}/federated-token`. Proves sovereign-identity federation (signed cross-realm
  mint) **without a 2nd SFU**. Fastest to a working back-and-forth call.
- **Shape B — two real SFUs:** stand up livekit-server on .41 (`5b80a6d0`) + cross-host Nostr discovery +
  runtime focus-advertise + reciprocal trust + federated agent-in-call (`64a50eec`). True peer federation.

---

## Epics, waves & tasks (coord board, tag `skchat-unify`)

### Wave 0 — Foundations / backend unblockers (parallel, start now)
- **F0-sync** Sync .41 skchat → .158 HEAD + reinstall `~/.skenv` (both boxes same federation code).
- **F0-jsonsend** Add JSON `/send` route to webui + confirm `/inbox` JSON contract for the app.
- **F0-fqid** Reconcile FQID realm mismatch (`@chef.skworld` vs `@skworld.io`); confirm `resolve_agent_identity()`
  emits the pinned fqid; align trust/pins both boxes.
- **F0-nostr** Rebind Nostr relay to tailnet IP on .158 + set `SKCHAT_NOSTR_RELAYS` on both boxes (also unblocks B).

### Wave 1 — App feature parity (the merge; big fan-out, worktree-isolated)
- **A1-hostswitch** Promote Spaces/LiveKit/Capstone URLs to **runtime settings** + in-app instance picker
  (lumina@.158 / jarvis@.41). *Mirror `DaemonConfigNotifier`.* ← KEY for federation UX.
- **A2-conf** Conf REST client + host-controls screen: `/conf/create`, `/conf/{room}/token`, participants,
  end, waiting-room admit/deny, invite-agent/remove-agent.
- **A3-deeplink** Deep-link join: guest (`/guest/join`) + sovereign (`/join/sovereign`, capauth-signed
  `{claim,sig}`) + chooser; URL/intent handler.
- **A4-reactions** Transport reactions + typing over the wire (send + receive paths).
- **A5-attach** Attachments parity: `POST /upload` + `GET /file/{id}` + thumb wired in input bar/bubbles.
- **A6-cluster** G6 Cluster-control screen (skbloom `/api/services|propose|up|status|health|restart|scale|logs` SSE).
- **A7-recordings** Recordings browser + record start/stop (`/recordings`, `/livekit/record/*`).
- **A8-facetime** Facetime avatar call path (`WS /ws/facetime/{agent}` + `/api/facetime/*`).
- **A9-watch** Watch-together native parity (replace `watch_video_stub.dart`).
- **A10-nav** Operator-hub nav: add `/coord` + cluster to nav; consolidate one-client hub.

### Wave 2 — Federation Shape A (ship "calls back and forth")
- **B1-fedclient** Wire the **conf federation CLIENT** caller: app mints cross-realm conf token via
  `/conf/{room}/federated-token` (signed assertion) — the missing client leg.
- **B2-jarvisinst** Fix `.41` to run a real **Jarvis app instance** (SKAGENT=jarvis, real creds/identity),
  app points at jarvis instance but joins .158 SFU via cross-realm token.
- **B3-webdeploy** Build + serve **Flutter web** on .158 (lumina) and .41 (jarvis); per-box services.
- **B4-e2eA** E2E: lumina@.158 ↔ jarvis@.41 join same conf on .158 SFU, **video+screenshare both ways**,
  driven via CDP browser (2 users) + unit/integration tests.

### Wave 3 — Federation Shape B (true two-SFU peer federation)
- **C1-sfu41** Stand up `livekit-server` on .41 (binary, config bound `100.86.156.5`, `skchat-jarvis` key,
  systemd + tailnet-wait + `tailscale serve :8443→7880`). (`5b80a6d0`)
- **C2-advertise** Runtime focus-advertise: wire `publish_focus`/`publish_membership` into conf/space create;
  cross-host Nostr discovery (`discover_and_elect`).
- **C3-reciprocal** Reciprocal trust: add `skchat-jarvis` key to .158; pin/trust lumina on .41.
- **C4-fedagent** Federated agent-in-call + federation observability/runbook. (`64a50eec`)
- **C5-e2eB** E2E: conf hosted on **.41's** SFU, lumina@.158 joins via discovery + cross-realm mint, and
  vice versa (true `1afbfb1a` proof).

---

## Execution
Worktree-isolated swarm agents (one branch each) in the two repos (`skchat-app`, `skchat`), merged
sequentially to `main`. Wave 0 + Wave 1 fan out in parallel; Wave 2 after A1/A2/A3/B-deps; Wave 3 last.
Repos: GitHub is master. Run skchat tests from `~` (not smilintux-org) to avoid skmemory namespace collision.
Related: [[sovereign-conf-calls]], [[lumina-everywhere-initiative]].
