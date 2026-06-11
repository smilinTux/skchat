# Spec: WebRTC Session After Pairing — Sub-project A (LiveKit-backed symmetric call)

**Date:** 2026-06-11
**Coord task:** `7f28ac51` (P2P: WebRTC session), phase-p2p
**Status:** Design — pending approval
**Parent decomposition:** A (this) → B (P2P direct) → C (layered fallback).
See `2026-06-11-nextcloud-talk-fit-decision.md` for why we build our own core.

## Goal
After two peers pair (skcomms TOFU), either of them — a human in a browser **or**
an agent — can start a real-time audio/video call with the other, over our existing
LiveKit server, with each participant carrying their **capauth identity**.

This sub-project delivers a *working symmetric call* and builds the reusable
scaffolding (deterministic room, agent-aware tokens, the `CALL_INVITE` envelope)
that sub-projects B and C depend on.

## Non-goals (deferred)
- **B:** true peer-to-peer media over skcomms WebRTC (no SFU).
- **C:** layered negotiation (try P2P, fall back to LiveKit) and the Talk-compat shim.
- Recording/egress changes, group calls (>2).
- **skmesh / netbird overlay** (connectivity tier 4) — designed-for but not wired in A.

## Connectivity — two planes
A common confusion to head off: **reachability** (how a client reaches skchat's
signaling/SFU HTTP/WS endpoints) and **media/ICE** (how WebRTC media traverses NAT) are
**different planes**. Cloudflare Tunnel lives in the first; coturn/TURN in the second.
They compose (e.g. cloudflared-exposed signaling + coturn-relayed media).

### Plane 1 — Reachability (how clients reach the service)
Deployment/ingress concern; A's code is agnostic (it just serves HTTP/WS). Options,
sovereign-first:
| Option | Exposure | Notes |
|---|---|---|
| **Tailscale serve** (priority) | tailnet-only HTTPS | current default; camera secure-context works |
| **Cloudflare Tunnel** (cloudflared → Traefik) | public HTTPS/WS | TCP only — fronts signaling + LiveKit `wss` signaling + the webui; **cannot** relay WebRTC UDP media (still needs TURN). The nativeassetmanagement cluster already fronts everything this way. |
| **Direct LAN / standalone host** | local network | homogenous network, no ingress |

### Plane 2 — Media / ICE policy (`skchat/connectivity.py`) — shared by A + B
A small transport-agnostic module producing the ordered ICE config + preferred tier:
| Tier | Path | When | ICE servers |
|---|---|---|---|
| 1 | **Tailscale** (priority) | both peers on the tailnet | none — tailnet handles NAT; direct |
| 2 | **Same-network / LAN** | homogenous local network | host candidates only |
| 3 | **coturn TURN** | cross-NAT / off-tailnet | STUN+TURN via the shared skstack coturn `skhub.<cluster>.<domain>:3478` (turn,turns / udp,tcp), the **same coturn used by Nextcloud Talk + netbird**. Secret = vault `skhub.turn_secret`. |
| 4 | **skmesh / netbird** | larger sovereign mesh | overlay-provided (DESIGN-FOR, not built in A) |

> **coturn host TBD-confirm:** the skstack v1 spreed config points TURN at
> `skhub.<CLUSTERNAME>.<DOMAIN>:3478` (e.g. `skhub.nativeassetmanagement.com:3478`); Chef
> recalled `signal.nativeassetmanagement.com`. Resolve at provisioning via env
> `SKCHAT_TURN_URLS` — do not hardcode.
> **coturn breakout (infra, separate):** coturn is currently bundled in the skhub stack
> (down ~1mo). Recommend breaking it out as a standalone service so TURN availability is
> decoupled from skhub. Tracked as an ops follow-up, not part of sub-project A code.

### `skchat/connectivity.py` (new)
```
ice_config(local_ctx, peer_ctx) -> {
    ice_servers: [ {urls:[...], username?, credential?}, ... ],
    policy: "all" | "relay",          # relay only when forced
    preferred_tier: 1|2|3,
    on_tailnet: bool,
}
```
- **Tier detection:** are both peers reachable on the tailnet (FQID/peer hint carries a
  `tail*.ts.net` or 100.64/10 addr)? → tier 1, empty `ice_servers`. Else same-subnet
  hint → tier 2. Else → tier 3 (emit STUN+TURN).
- **Tier 3 credentials:** coturn uses the **REST/`use-auth-secret`** ephemeral-credential
  scheme — `username = "<expiry-ts>:<peer>"`, `credential = base64(hmac-sha1(secret, username))`.
  The shared secret is sourced from config (`SKCHAT_TURN_SECRET` / skstacks secret),
  **NOT hardcoded**, and a TURN cred is short-TTL per call. 🔴 Credential hygiene: source
  a fresh secret scoped to skchat; never reuse cluster admin creds (cf. the NC_PASS lesson).
- **Exposure:** `GET /connectivity/ice?peer=<fqid>` returns the config for `livekit.html`
  (LiveKit `RoomOptions.rtcConfiguration.iceServers`) and, later, B's `RTCPeerConnection`.
- **LiveKit leg:** when the SFU itself must be reachable off-tailnet, the LiveKit server's
  `rtc.turn_servers` / external coturn is configured to the same `signal.…` host so the
  client↔SFU leg can relay. (A keeps LiveKit on the tailnet by default = tier 1; tier-3
  SFU exposure is configured but only engaged when a participant is off-tailnet.)

## Architecture

Five units, each independently testable:

### 1. Room derivation — `skchat/call_session.py` (new)
Pure function, no I/O:
```
derive_room(fqid_a: str, fqid_b: str) -> str
    # room = "call-" + base32(sha256("\n".join(sorted([fqid_a, fqid_b]))))[:16].lower()
```
- **Order-independent:** `derive_room(x, y) == derive_room(y, x)`.
- Deterministic → both sides compute the same room with zero negotiation.
- Opaque (hashed) → FQIDs are not leaked in the room name to the LiveKit server logs.
- Reused verbatim by C's fallback (the room both sides jump to when P2P fails).

### 2. Agent-aware token minting — extend `skchat/livekit_routes.py`
Existing `/livekit/token` already mints a JWT for `{identity, name, room}` from
`SKCHAT_LIVEKIT_API_KEY`/`SECRET`. We add a higher-level endpoint:

Two endpoints — **initiate** (rings the peer) and **answer** (no ring), so the
accept path never re-sends an invite (avoids an invite loop):
```
POST /call/start    body: {peer: "<fqid-or-name>"}     # caller, initiates
  1. resolve peer -> fqid via skcomms peers/TOFU registry; 404 if not paired
  2. local_fqid = capauth.resolve_agent_identity().fqid
  3. room  = derive_room(local_fqid, peer_fqid)
  4. token = mint_token(identity=local_fqid, name=<display_name>, room=room)
  5. send CALL_INVITE to peer over skcomms (unit 4)
  6. return {room, token, livekit_url, peer_fqid}

POST /call/answer   body: {peer: "<fqid>"}             # callee accept, NO ring
  1..4 identical (resolve, local_fqid, derive_room, mint_token)
  5. (optional) send CALL_ACCEPT back over skcomms — NOT a CALL_INVITE
  6. return {room, token, livekit_url, peer_fqid}
```
- `/call/start` and `/call/answer` share steps 1–4 (factor into `_prepare_call(peer)`);
  they differ only in which control message they emit (INVITE vs optional ACCEPT).
- Token **identity = local participant's capauth FQID** (identity-bound + distinguishable:
  `lumina@chef.skworld` vs `opus@chef.skworld` vs the human).
- `display_name` = the agent name or the human's paired display name.
- `livekit_url` from `SKCHAT_LIVEKIT_URL` (already in env).

### 3. opus LiveKit provisioning (config, not code)
- Add `skchat-opus: <fresh-secret>` to `~/.config/livekit/livekit.yaml` `keys:`.
- Add `SKCHAT_LIVEKIT_URL` / `SKCHAT_LIVEKIT_API_KEY=skchat-opus` /
  `SKCHAT_LIVEKIT_API_SECRET` / `SKCHAT_LIVEKIT_DEFAULT_ROOM` to `webui-opus.env`.
- `livekit-server.service` reload after the key add.
- (Per-agent key keeps token issuance attributable per agent; the LiveKit server
  trusts both keys.)

### 4. Ring / call-invite — `CALL_INVITE` over skcomms
The one genuinely new transport bit. The callee learns to join via a control
message on the **existing post-pairing skcomms channel** (not a new socket):
```
CALL_INVITE envelope (JSON):
  { type: "CALL_INVITE", from_fqid, to_fqid, room, transport: "livekit",
    livekit_url, ts, nonce }
```
- **Send:** `/call/start` step 5 → `skcomms.transport.send(peer, CALL_INVITE)`.
- **Receive:** callee's webui polls its skcomms inbox (reuse the existing inbox
  poll the pairing flow already uses) → surfaces an **incoming-call banner/ring**
  in the webui with Accept / Decline.
- **Accept:** callee calls `/call/answer` for the *same* peer (recomputes the same
  deterministic room locally from the two FQIDs — the room in the envelope is only a
  cross-check, never trusted) → mints its own token → opens `livekit.html` joined to
  that room. `/call/answer` does **not** emit a CALL_INVITE, so there is no ring loop.
- **Security:** the room in the envelope is advisory; each side derives the room
  independently from the paired FQIDs, so a forged room cannot redirect a callee.
  `to_fqid` must match the receiver's own FQID or the invite is dropped.

### 5. Trigger UX (webui)
- Paired-peers list: each peer row gets a **Call** button → `POST /call/start` →
  open `livekit.html?room=<room>&identity=<local_fqid>` (reuse the working page;
  it already does token fetch + LiveKit connect, just pass room+identity).
- Incoming `CALL_INVITE` → ring banner (Accept/Decline) wired to the same flow.
- Agent side (headless): an MCP tool `call_peer(peer)` that runs `/call/start`
  server-side and launches the lumina join-agent (`scripts/lumina-join-call.py`,
  parameterized by `--room` + `--identity`) so an agent can answer/place a call.

## Data flow (happy path: human-on-phone calls lumina)
```
Phone (paired) --Call--> /call/start{peer:lumina}
  -> derive_room(human_fqid, lumina_fqid) = call-xxxx
  -> mint token(identity=human_fqid, room=call-xxxx)
  -> skcomms.send(lumina, CALL_INVITE{room:call-xxxx})
  -> returns {room, token} ; phone opens livekit.html -> joins call-xxxx
lumina webui inbox poll -> CALL_INVITE -> ring banner
  -> accept -> /call/answer{peer:human} -> same room call-xxxx (recomputed) -> join
Both in room call-xxxx on LiveKit -> media flows.
```

## Error handling
- LiveKit not configured / unreachable → `/call/start` 503 (existing `_have_creds`).
- Peer not in TOFU registry → 404 "not paired".
- `CALL_INVITE` with `to_fqid` ≠ self → dropped (logged).
- Token TTL = existing default (6h, env-tunable). Empty rooms auto-reaped by LiveKit.
- Callee declines → optional `CALL_DECLINE` envelope back (nice-to-have; caller just
  times out otherwise).

## Testing
Unit:
- `derive_room` order-independence + stability + format.
- `/call/start` rejects unpaired peer (404); mints token with identity=local FQID.
- `CALL_INVITE` envelope serialize/deserialize; `to_fqid` mismatch drop.
Integration:
- opus + lumina paired → opus `/call/start{peer:lumina}` then lumina `/call/answer{peer:opus}`
  → assert both tokens decode to the **same room** and carry distinct identities;
  assert `/call/start` emitted exactly one CALL_INVITE and `/call/answer` emitted none.
- CALL_INVITE delivered opus→lumina over skcomms inbox (mock transport).

## Files touched
- new `skchat/call_session.py` (derive_room, CALL_INVITE model)
- new `skchat/connectivity.py` (ICE policy ladder; `ice_config`, `/connectivity/ice`)
- edit `skchat/livekit_routes.py` (`/call/start`, `/call/answer`)
- edit `skchat/webui.py` (Call button + ring banner + inbox surfacing + pass ICE to livekit.html)
- edit `skchat/mcp_server.py` (`call_peer` tool)
- config: `~/.config/livekit/livekit.yaml` (skchat-opus key + optional `rtc.turn_servers`),
  `~/.config/skchat/webui-opus.env`, `SKCHAT_TURN_SECRET` (sourced, not committed)
- tests under `skchat/tests/`

## Connectivity testing (added)
- `ice_config`: both-on-tailnet → tier 1, empty ice_servers; cross-NAT → tier 3 with a
  TURN entry whose `username` is `<expiry>:<peer>` and `credential` = HMAC of the secret.
- TURN credential is ephemeral (TTL) and the secret is never emitted in logs/responses
  beyond the derived short-lived credential.
- `/connectivity/ice` returns tier-appropriate config for a paired peer; rejects unpaired.
```
