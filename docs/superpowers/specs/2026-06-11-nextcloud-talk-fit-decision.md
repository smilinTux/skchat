# Decision: Nextcloud Talk fit vs. sovereign WebRTC core

**Date:** 2026-06-11
**Status:** Decided — build sovereign core (LiveKit + P2P), defer Talk bridge as a stub.
**Source:** research swarm `wf_bba6e407-632` (14 agents, ~721k tokens, 8 load-bearing
claims adversarially verified).

## Question
Should skchat's "WebRTC session after pairing" leverage Nextcloud Talk (spreed) —
its battle-tested android/ios/desktop clients, or a drop-in backend — instead of /
in addition to building our own?

## Verdict
Leverage Talk only as an **additive, chat-only, human-reachable surface** — never the
call engine, never a reimplementation, never an identity authority. Build our own
sovereign call core. Key finding: the spreed media tier (strukturag signaling + Janus)
is backend-agnostic, but **LiveKit (which we already run) does the same thing** — a
capauth-signed JWT carrying identity into the media plane, the same primitive as spreed
protocol-v2 — in one mature service. Re-platforming onto Talk would add friction, not
remove it.

## Strategy ranking
1. **Build our own core** (LiveKit SFU + P2P + capauth/FQID/QR pairing) — effort **M**,
   full sovereignty. Core largely shipped (`livekit_routes.py` mints JWTs). **CHOSEN.**
2. **Chat-only bridge** to Talk on Chef's own Nextcloud — effort **S**, partial
   sovereignty, stock clients free. **DEFERRED to a stub** (see below).
3. **Federation seam** (OCM federated peer) — effort L. Immature (single-host-per-room,
   no federated attachments/moderators, cross-version breakage). Defer.
4. **Drop-in backend** (reimplement spreed OCS) — effort **XL**. ~186 capability flags
   drift every release; clients hardcode Nextcloud routes; AGPL-3.0 blocks code reuse.
   Rejected unless "stock Talk clients on our backend" becomes non-negotiable (it isn't).

## Impact on the A→B→C call-stack plan
Survives intact. **A** (LiveKit symmetric call) = confirmed sovereign core, elevated to
primary. **B** (P2P direct) = tailnet-native small-room path. **C** (layered fallback)
also becomes the natural home for a future Talk-compat signaling shim onto our LiveKit
rooms. **Build A+B now; drop the spreed-reimplementation idea.**

## Deferred: S1 Talk chat-bridge (STUB — do not build yet)
When we're ready (post-core), a chat-only FQID↔Talk-room bridge is a fast win:
- **Test host:** `skhub.nativeassetmanagement.com` — `skhub.skstack01.douno.it` is
  down for a month+ (do not target it). Eventual real target = Chef's files instance
  `dkloud.douno.it`.
- An existing text-only spreed integration lives on this box (`talk.sh`/`talk-full.sh`)
  but points at skhub.
- **🔴 Credential hygiene:** that integration uses the `NC_PASS` flagged in the leaked
  app-password remediation. Any bridge MUST mint a **fresh, separately-scoped bot secret**
  (`occ talk:bot:install`, HMAC webhook) — never reuse the leaked one.
- Map FQID ↔ {nc_room, nc_actor} as new fields on skcomms `peers.json` (one-way lookup;
  capauth stays the sole identity root).

## Open risks carried forward
- **TURN/STUN gap:** NAT traversal (coturn/eturnal) unprovisioned for *both* LiveKit and
  P2P paths. Fine on the tailnet; needed before off-tailnet "production" calls.
- **protocol-v2 issuer unproven:** whether an arbitrary capauth-only issuer can drive
  spreed signaling with no Nextcloud-side registration is genuinely unverified (the JWT
  key is fetched from the NC capabilities endpoint `hello-v2-token-key`). Only matters if
  S1/C is ever pursued.
- Two NC instances (skhub cluster vs dkloud files) + 3 installed NC MCP servers — be
  explicit about which instance every credential/room token targets.
