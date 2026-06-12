# skchat Architecture Reassessment & v2 Roadmap

**Date:** 2026-06-12
**Author:** architect pass (Lumina/Opus session)
**Status:** Strategic direction ratified by Chef; batches pending scheduling

---

## 1. Strategic decision (ratified)

**skchat = the SKWorld real-time comms PRODUCT** (chat + voice + video), built
**Tailscale-native** on top of a **lightweight sovereign hub (skcomms)**, with
**LiveKit** as the shared real-time media plane.

Ratified refinements (Chef, 2026-06-12):
- **Tailscale-first.** Everything operates over the tailnet by default; public
  ingress (Cloudflare Tunnel / Tailscale Funnel) is *optional, per-deployment* —
  not required for the system to work.
- **No heavy homeserver.** We do **not** depend on Matrix/Tuwunel as the
  substrate (Postgres + federation + state-resolution + per-bridge appservices is
  too heavy). `skcomms` — already a multi-channel sovereign layer (17 paths) — is
  the hub.
- **Bridges are lightweight adapters**, not a homeserver. The "reach Lumina from
  Telegram / Slack / Discord / NC-Talk / Teams" vision is realized by pluggable
  **channel adapters** in skcomms (generalizing the working Telegram path), each
  a small connector — no Matrix required.
- **Matrix is one optional adapter**, used *only if* it makes sense (you
  specifically want Element clients or Matrix federation) — never the foundation.

**Naming/FQDN:** keep `skchat`. All routes are subdomains of owned apexes
(`skchat.skworld.io`, the tailnet name, `*-skstack01.douno.it`) — zero new domains.

## 2. Target architecture (4 layers, Tailscale-native)

```
┌─ skchat ───── the comms PRODUCT: agent-native UX (web/mobile), MCP tools,
│               the unified voice_engine (STT→LLM→TTS, persona, tools),
│               multi-agent roundtable.
│
├─ LiveKit ──── the ONE real-time media plane (voice + video), Tailscale-served
│               (wss over the tailnet; coturn only for off-net peers).
│
├─ skcomms ──── the lightweight SOVEREIGN HUB:
│                 • identity (capauth / FQID, signed envelopes, optional E2EE)
│                 • transport (P2P + mailbox)
│                 • CHANNEL ADAPTERS (pluggable): Telegram, Slack, Discord,
│                   NC-Talk, Teams, …, and Matrix as an *optional* adapter.
│               This is the "bridge" — no homeserver, just connectors.
│
└─ Tailscale ── the mesh everything runs over. Sovereign, encrypted, no public
                ingress required. Funnel/Cloudflare are opt-in exposure only.
```

**Why this is right (and lighter)**
- skcomms already does multi-channel; formalizing a **channel-adapter interface**
  turns the bespoke Telegram path into a clean, repeatable pattern → Slack,
  Discord, NC-Talk, etc. are each a small adapter, not an appservice.
- Tailscale gives sovereign, encrypted reachability with valid TLS (serve) and
  zero public surface — the call tier ladder already starts at Tailscale.
- LiveKit is the one media plane; coturn is only the tier-3 relay for off-net.
- capauth/FQID stays the sovereign identity root; agents are first-class on every
  adapter.
- Matrix stays available as an adapter (mautrix bridges) for the day federation
  or Element clients are worth the weight — opt-in, not load-bearing.

**Trade-off acknowledged:** skcomms adapters are bespoke per platform (vs Matrix's
"run the homeserver, get N bridges"). For a sovereign, Tailscale-native, low-ops
fleet that already has skcomms + a working TG path, lightweight adapters win;
Matrix-as-adapter remains the escape hatch if the bridge count ever justifies it.

## 3. Current state (what exists, 2026-06-12)

**Built & running (systemd, this box):**
- `skchat-daemon` (:9385/:9384) — SKComm receive loop, history, advocacy.
- `skchat-webui@<agent>` (:8765 lumina / :8766 opus) — FastAPI UI + voice WS +
  `/livekit/*` token mint + `/call/*` ring/answer + `/pair` gate.
- `skchat-lumina-call` — LiveKit conversational agent (the "skvideo" path).
- `skvoice` (:18800) — voice orchestrator (to be folded into voice_engine).
- `jarvis-heartbeat`, `skchat-mcp` (40-tool stdio MCP).
- **voice_engine Phase 1** (`src/skchat/voice_engine/`) — library, unit+live tested.
- **Multi-agent roundtable** (today) — two agents converse in a LiveKit room with
  loop-damping + addressing discipline; Chef joins over the tailnet.

**Media/connectivity (already Tailscale-native):** LiveKit SFU (:7880/:8443 via
`tailscale serve`), coturn (:3478, tier-3 of the Tailscale→LAN→coturn ICE ladder),
Tailscale Funnel for *optional* gated public pairing. The webui is reached at the
tailnet name today.

**External deps:** STT whisper (.100:18794), TTS kokoro-proxy (:15091), LLM proxy
(:18783 haiku → .100:8082 qwen3.6-ablit fallback), skmem-pg (:5432), skcomms.

**The two-brain debt:** skvoice (WebSocket) and lumina-call (LiveKit) are two
independent STT→LLM→TTS pipelines — the unified voice_engine consolidates them
(spec `2026-06-12-unified-voice-engine-design.md`).

**Current Telegram reality (to generalize):** Lumina is in the DR-Chiro TG group
via **Hermes**, which writes to skmem-pg. This bespoke path is exactly what the
skcomms channel-adapter pattern should absorb.

## 4. v2 deployment target (Tailscale-native first)

- **Orchestration:** Docker Swarm; app = `v2/apps/<app>/stack/<app>-stack.yml`,
  `docker stack deploy`.
- **Reachability:** **tailnet by default** (services on the Tailscale CIDR; TLS via
  `tailscale serve`). Traefik + Cloudflare Tunnel are an *optional* public edge for
  the few routes that need it (e.g. public pairing) — not the default path.
- **Secrets:** env from host `.env` (`/var/data/deploy_<app>/`), migrating to
  **OpenBao**. LiveKit/TURN/model secrets MUST leave the image.
- **Storage:** skchat core mostly stateless; state (history, recordings, capauth
  keys) → volumes + Garage backup.
- **Contract per app:** Dockerfile + `/health` + stack YAML (tailnet/Traefik
  routing, overlay network, placement, `.env`-sourced secrets).

## 5. Reprioritized roadmap — work batches

### Batch A — Finish the unified voice engine (consolidate the two brains)
*In-flight; highest near-term value; precondition for clean deployability.*
- **A1** Phase 2: rewire web-chat onto `voice_engine` + the **tool registry**
  (memory, narrate, worship, reflections, **bloom**) + forced-tool routing (ported
  from today's lumina-call fixes); retire `skvoice`.
- **A2** Phase 3: rehome `lumina-call.py` → `skchat/transports/livekit.py` over the
  engine (VAD/barge-in/avatar/roundtable preserved) — finally version-controlled.
- **A3** Phase 4: one `VoiceConfig` both transports source; web↔video toggle.

### Batch B — v2 deployability, Tailscale-native (package what exists)
- **B1** Dockerfiles: `skchat-webui`, the `voice_engine` service (ex-skvoice),
  `skchat-daemon`. `/health`, non-root.
- **B2** `v2/apps/skchat/stack/skchat-stack.yml` — **tailnet routing first**
  (tailscale serve / tsnet sidecar), Traefik labels only for opt-in public routes.
- **B3** Secrets off the image (LiveKit secret, `SKCHAT_TURN_SECRET`, model keys) →
  `.env`/OpenBao; document the contract.
- **B4** Deploy **LiveKit** + **coturn** as first-class v2 stacks (the shared media
  plane), Tailscale-served, instead of hand-run processes.
- **B5** State volumes (history/outbox/recordings/identity keys) + Garage backup.

### Batch C — skcomms channel-adapter framework (the lightweight "bridge")
*The pivot — sovereign, no homeserver.*
- **C1** Define the **ChannelAdapter interface** in skcomms (inbound + outbound +
  identity mapping FQID↔platform-user + media hint). One clean contract.
- **C2** **Telegram adapter** — generalize the working Hermes/DR-Chiro path into a
  proper adapter; Lumina reachable in TG groups, history → skcomms + skmemory.
- **C3** Adapter registry + routing: an agent (Lumina/Opus) appears across all
  enabled adapters under one FQID; the voice_engine answers regardless of channel.
- **C4** **Slack** + **Discord** adapters (next most valuable).
- **C5** **Matrix adapter (optional)** — only if Element/federation is wanted; this
  is where mautrix-style reach plugs in without making Matrix load-bearing.

### Batch D — Reach + clients
- **D1** NC-Talk + Teams adapters (more custom; later).
- **D2** skchat web/mobile client polish over the tailnet (the SKWorld UX).

### Batch E — Multi-agent + hardening
- **E1** Promote today's roundtable to durable `skchat-agent@<name>` stack
  services (replace hand-run `run-agent.sh`).
- **E2** Observability (skmon/Loki), live signaling-broker deploy, P2P fallback as
  a v2 component.

## 6. Recommended sequence

**A → B → C → D**, E threaded through.

- **A** first: one clean agent brain (folds in today's fixes) before we Dockerize a
  duplicated mess.
- **B**: it runs on the cluster, Tailscale-native — immediate deployable win.
- **C**: the sovereign lightweight bridge (skcomms adapters) — realizes the
  multi-platform vision without a homeserver.
- **D/E**: reach + hardening.

Each batch gets its own spec → plan before build (superpowers flow).
