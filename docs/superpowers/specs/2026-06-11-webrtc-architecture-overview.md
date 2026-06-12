# SKChat WebRTC — Architecture Overview & Design Record

**Date:** 2026-06-11
**Scope:** "WebRTC session after pairing" (coord `7f28ac51`) — the full sovereign
real-time-comms stack: pairing → signed call signaling → media (SFU + P2P) → the
agent-native-comms-language north star.
**Status legend:** ✅ shipped · 🟡 designed/in-progress · ⚪ planned

This is the canonical map. Per-piece detail lives in the sibling specs/plans:
- `2026-06-11-skchat-webrtc-session-A-design.md` + `…-A.md` (plan) — ✅ merged (PR #4)
- `2026-06-11-skchat-webrtc-session-B-design.md` — 🟡 spec'd
- `2026-06-11-nextcloud-talk-fit-decision.md` — the build-our-own decision
- skcomms signing fix — ✅ merged (skcomms PR #5)

---

## 1. Where we are in the stack

```mermaid
graph TD
    subgraph Identity["🔑 Identity & Trust (capauth + skcomms)"]
        CA["capauth identity<br/>per-agent keypair<br/>~/.skcapstone/agents/&lt;a&gt;/capauth/identity"]
        TOFU["TOFU peer registry<br/>fingerprint-pinned trust"]
        SIGN["EnvelopeSigner / verify<br/>✅ fixed: signs with AGENT key"]
        CA --> SIGN
        CA --> TOFU
    end

    subgraph Pairing["📷 Pairing (skcomms.pairing) ✅"]
        QR["skp:// QR + /pair, /pair/scan<br/>verify-fp-before-TOFU-add"]
    end

    subgraph Signaling["📞 Call signaling (skchat) ✅ A"]
        ROOM["call_session.derive_room()<br/>deterministic per-pair room"]
        INVITE["CALL_INVITE over signed skcomms<br/>/call/start · /call/answer · /call/incoming"]
        ICE["connectivity.ice_config()<br/>tier ladder"]
    end

    subgraph Media["🎥 Media plane"]
        LK["LiveKit SFU ✅ A<br/>livekit_routes + livekit.html<br/>tailnet wss :8443"]
        P2P["P2P direct 🟡 B<br/>aiortc · data+audio+video<br/>no server"]
        FALL["layered fallback ⚪ C<br/>P2P → SFU"]
    end

    subgraph Reach["🌐 Reachability (skstacks v2 exposure)"]
        TS["Tailscale serve (default)"]
        CF["Cloudflare Tunnel"]
        NB["netbird mesh"]
        TURN["coturn apps/skturn<br/>shared w/ NC-Talk+netbird"]
    end

    Identity --> Pairing --> Signaling
    Signaling --> Media
    ICE -.tier 3.-> TURN
    Media --- Reach
    LK -.fallback.-> FALL
    P2P -.fallback.-> FALL

    NORTH["🧠 agent-native comms language ⚪<br/>structured protocol over the P2P data channel"]
    P2P --> NORTH
```

## 2. Trust & signing flow (the sovereign guarantee)

Every cross-agent message is capauth-signed and verified against the peer's
TOFU-pinned fingerprint. **The 2026-06-11 fix:** agents were signing with the
*operator* key (wrong path) so nothing verified — now each agent signs with its own
`capauth/identity` key. This is what makes "trust level by profile" real.

```mermaid
sequenceDiagram
    participant O as opus (signer)
    participant M as skcomms mailbox
    participant L as lumina (verifier)
    Note over O: _load_signer(opus)<br/>→ capauth/identity/private.asc (6136E987)<br/>NOT the operator key
    O->>O: build_envelope + EnvelopeSigner.sign(SDP/CALL_INVITE)
    O->>M: signed envelope → lumina inbox
    M->>L: read_inbox() → (Envelope, VerificationResult)
    Note over L: _find_key(opus) from TOFU (6136E987)<br/>verify signature over canonical bytes
    alt signature valid
        L->>L: surface ring / accept SDP
    else invalid / unsigned
        L--xL: DROP (the /call/incoming gate)
    end
```

## 3. Connectivity — two planes + the tier ladder

Reachability (how a client reaches the HTTP/WS) and media/ICE (how WebRTC traverses
NAT) are **different planes**. Cloudflared can front signaling but never relays UDP media.

```mermaid
graph LR
    subgraph P1["Plane 1 — Reachability (TCP/HTTP/WS)"]
        direction TB
        R1["Tailscale serve<br/>(tailnet-only, default)"]
        R2["Cloudflare Tunnel<br/>(public, → Traefik)"]
        R3["LAN / standalone"]
    end
    subgraph P2["Plane 2 — Media / ICE (UDP)"]
        direction TB
        T1["Tier 1: Tailscale<br/>(both on tailnet → direct)"]
        T2["Tier 2: LAN host candidates"]
        T3["Tier 3: coturn TURN<br/>(skturn, ephemeral REST creds)"]
        T4["Tier 4: netbird mesh ⚪"]
        T1 --> T2 --> T3 --> T4
    end
    P1 -. "fronts signaling only" .-> P2
```

## 4. Call-after-pairing sequence (A shipped, B designed)

```mermaid
sequenceDiagram
    actor Caller as opus / Chef-browser
    participant CS as call_session/derive_room
    participant SK as signed skcomms
    actor Callee as lumina
    participant MX as media (SFU ✅ / P2P 🟡)

    Caller->>CS: /call/start(peer)
    CS->>CS: room = derive_room(a,b) — identical both sides
    CS->>MX: mint token (id = capauth FQID) [A]  /  createOffer+SIGN [B]
    CS->>SK: CALL_INVITE (+ SDP_OFFER for B), signed
    SK->>Callee: /call/incoming ring (sig-verified)
    Callee->>CS: /call/answer(peer) — recomputes SAME room
    CS->>SK: (CALL_SDP_ANSWER signed, for B)
    Caller->>MX: join room [A] / ICE connect [B]
    Callee->>MX: join room [A] / ICE connect [B]
    MX-->>Caller: media flows (SFU relay [A] / direct P2P [B])
    MX-->>Callee: data channel + audio (+video) 
    Note over MX: ICE fails on all tiers → C falls back P2P→SFU (same room)
```

## 5. The A → B → C decomposition

```mermaid
graph LR
    A["A · LiveKit SFU call ✅<br/>deterministic room · signed ring<br/>call_session/connectivity/call_routes<br/>call_peer MCP · webui Call+ring<br/>848 tests · PR #4 merged"]
    B["B · Sovereign P2P 🟡<br/>data+audio+video · no SFU<br/>dual signaling (mailbox+broker)<br/>signed SDP · ICE ladder<br/>finish/wire the aiortc stack"]
    C["C · Layered fallback ⚪<br/>P2P→SFU on ICE failure<br/>+ Talk-compat shim"]
    NL["🧠 agent comms language ⚪<br/>protocol over P2P data channel"]
    A -->|"reusable: room, ring, ICE, signing"| B
    B -->|"P2P half of the pair"| C
    A -->|"SFU half of the pair"| C
    B --> NL
```

## 6. Component → file map

| Concern | Module(s) | Repo | State |
|---|---|---|---|
| Deterministic room + CALL_INVITE | `call_session.py` | skchat | ✅ |
| ICE tier ladder | `connectivity.py` | skchat | ✅ |
| Call routes (`/call/*`, `/connectivity/ice`) | `call_routes.py` | skchat | ✅ |
| `call_peer` MCP tool | `mcp_server.py` | skchat | ✅ |
| Call UI (Call btn, ring banner, peers) | `webui.py` | skchat | ✅ |
| LiveKit join page (qp room/identity/token + ICE) | `static/livekit.html` | skchat | ✅ |
| LiveKit token mint | `livekit_routes.py` | skchat | ✅ |
| Pairing (QR/TOFU) | `pairing.py` | skcomms | ✅ |
| **Per-agent signing key** | `mailbox.py`, `grants.py` | skcomms | ✅ (PR #5 fix) |
| P2P transport (data channel ✅, media/sign 🟡) | `transports/webrtc*.py` | skcomms | 🟡 B |
| Mailbox signaling (sovereign SDP/ICE) | `transports/signaling_mailbox.py` | skcomms | ✅ B |
| Broker signaling (fast path) | `transports/signaling_broker.py` + `signaling_base.py` | skcomms | ✅ B (live-broker validation pending) |
| P2P session (data+audio+video) | `transports/p2p_session.py` | skcomms | ✅ B |
| P2P connector / **session manager** | `transports/p2p_connector.py` / `p2p_manager.py` | skcomms | ✅ B |
| skchat P2P glue + MCP tools | `p2p_calls.py` (`p2p_call/listen/status/send`) | skchat | ✅ B |
| **Layered fallback** (P2P→SFU) | `call_orchestrator.py` (`call_auto` tool) | skchat | ✅ C |
| Operator observability (alert+join) | `call_observability.py` (topic + sk-alert) | skchat | ✅ (e8651a65) |
| coturn standalone | `apps/skturn` | SKStacks v2 | 🟡 (other session) |

## 7. Design decisions (log)

1. **Build our own core, don't reimplement Nextcloud Talk.** Research swarm verdict: Talk
   clients are tightly server-coupled; a drop-in backend is XL/permanent-tax; LiveKit
   already gives the same "signed-JWT identity into the media plane" as spreed. Talk stays
   a *deferred additive chat bridge* (test host `skhub.nativeassetmanagement.com`).
2. **Deterministic per-pair room** (hash of sorted FQIDs) → zero-negotiation room agreement;
   also the free landing zone for C's fallback.
3. **Identity = capauth FQID** in every token/credential; **verify-before-add** /
   **verify-before-surface** on every hop (sovereign trust by profile).
4. **Two planes** (reachability vs media) — cloudflared for signaling, coturn for media.
5. **coturn standalone** (`apps/skturn`), decoupled from the month-down skhub stack; shared
   secret with NC-Talk + netbird.
6. **"If you need one, get two"** — dual signaling (mailbox+broker), three media modalities
   (video graceful-degrades), full ICE ladder, P2P+SFU transports. No single point of failure.
7. **Signed SDP** (B) reuses the per-agent signing fix — no MITM on media negotiation.

## 8. Live status (2026-06-12)
- **A (LiveKit SFU) shipped** (skchat PR #4, tag `webrtc-A-v1`). **B (sovereign P2P) +
  C (P2P→SFU fallback) shipped** → main (tag `webrtc-BC-v1`). skcomms 145 + skchat 863 tests.
- Verified: signed CALL_INVITE ring end-to-end; signed mailbox SDP signaling (opus→lumina);
  direct P2P data+audio channel (aiortc loopback); manager auto-answer; `call_auto` fallback.
- MCP tools live: `call_peer` (SFU), `p2p_call`/`p2p_listen`/`p2p_status`/`p2p_send` (P2P),
  `call_auto` (P2P-first + SFU fallback). Operator alert (topic + one-press join) on `/call/start`.
- Browser media leg proven; **Lumina's conversational agent in `lumina-and-chef`** (audio +
  77 MCP tools). Runbooks: `qr-pairing-phone-test.md`, `browser-call-test.md`.
- **Open:** live-broker validation of BrokerSignaling (needs the skcomm daemon broker up);
  Tailscale Funnel public pairing (`2ab5aa6c`, outward-facing — operator-gated). **🔴 live
  voice blocked by `.100` F5-TTS on the wedged Arc iGPU (no CUDA for the 5060 Ti).**
