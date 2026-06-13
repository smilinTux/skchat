# SK Spaces — sovereign, federated, audio-only community rooms

**Date:** 2026-06-13
**Repo:** `skchat` (feature home) + `skcomms` (federation discovery)
**Status:** design approved — ready for implementation plan
**Builds on:** skchat LiveKit call infra, `guest.py`, the 2027 design system, skcomms `nostr` transport + capauth/FQID identity

---

## 1. Goal

X-Spaces-style **live audio rooms for communities**, but sovereign and federated:
a host opens a Space, speakers talk, a large audience listens, listeners raise a
hand to be invited up. No Big-Tech server — each Space runs on a **sovereign
LiveKit SFU on the tailnet**, and independent SK hosts **federate by FQID
identity** so a Space on one host is reachable from another.

**Decided scope (Chef, 2026-06-13):**
- **Build both** the single-host core *and* the federation layer (federation is
  in-scope to build, not just designed-for-later). Single-host ships first; the
  federated layer lands on top.
- **Listeners = members + guest-link.** FQID members/agents plus anyone holding a
  signed invite link (reuses `guest.py`) can listen. Speaking always requires a
  host invite.

## 2. The load-bearing decision: SFU, never mesh

Researched against X Spaces, Discord Stage, Clubhouse, Element Call/MatrixRTC,
Jitsi, and Nostr NIP-53. The universal truth:

- **Full WebRTC mesh dies at ~5–6 participants** (N² connections; a speaker would
  need thousands of uplinks for a crowd). Categorically impossible for Spaces.
- **Every serious product uses an SFU** (Selective Forwarding Unit): each speaker
  sends **one** uplink; the SFU fans it out to all listeners. A Space's shape is a
  *broadcast asymmetry* — a few publishers in, a huge subscribe-only audience out
  — which is the cheapest possible SFU load.
- **One LiveKit SFU node carries ~1,000 audio listeners / ~10 active speakers at
  ~80% CPU on 16 cores (~23 MBps egress)**; hard ceiling ~3,000/room. A room must
  fit on a single node — multi-node LiveKit scales the *number of rooms*, not one
  room's size. Beyond a few thousand listeners you add an **HLS-egress fan-out
  tier** for the passive long tail (S6, parked).

**So "the central server" = one sovereign SFU per Space, tailnet-bound, no public
ingress.** That is correct and unavoidable per-room. "Decentralized" means **many
sovereign hosts each running their own SFU + auth service, federated by FQID
identity** — exactly the shipping MatrixRTC/Element Call model (§7).

## 3. What we reuse (recon: almost everything already exists)

| Need | Existing piece | Reuse |
|---|---|---|
| Room name | `call_session.derive_room()` | extend to a Space id (hash of host FQID + slug) |
| Join token | `livekit_routes._mint_token()` | extend with **role grants** (§4) |
| Guest listeners | `guest.py` `InviteIssuer`/`InviteVerifier`/`build_livekit_token` | reuse; scope to a Space + listener perms |
| Connectivity | `connectivity.ice_config()` tier ladder | as-is (tailnet → LAN → coturn) |
| Deploy | `deploy/v2/livekit-stack.yml` + `coturn-stack.yml` | as-is; one SFU serves many Spaces |
| Text backchannel | data-lane model (`{lane:"chat"}`) | reuse the chat lane in a Space |
| Discovery/presence | skcomms `heartbeat` + `nostr` transport | Space registry + federated discovery (§6, §7) |
| Identity/roles | `2026-06-13-identity-roles-access.md` (member/operator/guest tiers + perms) | extend with host/speaker/listener |
| UI language | 2027 design system | every Space surface |

Spaces is mostly **composition + extension**, not new infrastructure.

## 4. Roles & the speaker/listener switch

A Space is a LiveKit room with a three-tier role model, enforced by **token
grants** (initial) + **server-side `update_participant`** (runtime). The single
switch is `canPublish`.

| Role | LiveKit grant | Notes |
|---|---|---|
| **Host** | `roomJoin, canPublish, canSubscribe, canPublishData, roomAdmin` | opens/closes the Space, moderates, records |
| **Co-host / Moderator** | as host minus `roomRecord` (policy) | host-delegated |
| **Speaker** | `roomJoin, canPublish, canPublishSources:["microphone"], canSubscribe, canPublishData` | **mic only** — no camera/screen into an audio room |
| **Listener** | `roomJoin, canPublish:false, canSubscribe, canPublishData:true` | subscribe-only, **but can raise hand / react / chat** |

Token minting extends `_mint_token` to take a `role` and emit the matching grant.
Listener tokens come from the **member path** (FQID) or the **guest path**
(`guest.py`, signed invite link).

**Promote / demote at runtime, no rejoin.** `RoomServiceClient.update_participant`
mutates `ParticipantPermission.canPublish` live; the client receives
`ParticipantPermissionsChanged` and calls `setMicrophoneEnabled(true)` when
promoted. Demote sets `canPublish:false` and the SFU **auto-unpublishes** the mic.
This one primitive powers promote, demote, force-mute, and kick.

## 5. Raise-hand = mutual consent

Adopt LiveKit's own livestream pattern (two-flag AND-gate), carried in participant
**metadata** (durable) + the **data channel** (transient reactions):

- Listener raises hand → `metadata.hand_raised = true`.
- Host invites → `metadata.invited_to_stage = true`.
- **When both are true → `canPublish` flips true.** A user only goes live when the
  host invited *and* the user consented — mutually agreed, neither side unilateral.
- `removeFromStage` (host or self) resets both flags + `canPublish:false`.

All three transitions are a single `update_participant(room, identity, metadata,
permission)` call — metadata + permission updated atomically. The "✋ queue" UI
renders from `ParticipantMetadataChanged`.

**Role integrity (sovereign hardening):** following NIP-53, a speaker grant should
be **mutually signed** — the host's invite and the speaker's acceptance are both
capauth-signed events, so a host can't fraudulently list someone as a speaker.
The AND-gate above is the runtime form; the signed events are the federated form
(§7).

## 6. Single-host Space — components & flow

**New module `src/skchat/spaces/`:**

| File | Responsibility |
|---|---|
| `space.py` | `Space` model (id, host_fqid, title, status open/live/ended, created, mode) + `derive_space_id(host_fqid, slug)` |
| `roles.py` | role→grant mapping; `grant_for(role)`; the host/speaker/listener perm sets |
| `tokens.py` | `mint_space_token(identity, role, space_id, ttl)` (extends `_mint_token`) |
| `moderation.py` | `promote`, `demote`, `mute`, `kick`, `raise_hand`, `invite_to_stage`, `remove_from_stage` — all over `RoomServiceClient.update_participant` |
| `registry.py` | local "live now" Space registry (open Spaces on this host) + state for the directory UI |
| `routes.py` | `/spaces` REST: create, join (member), join (guest), list-live, raise-hand, invite, remove, mute, kick, end, start/stop-recording |
| `recording.py` | Egress room-composite **audio_only** start/stop; consent gating (§8) |
| `static/space.html` | the audio-room web UI (2027): speaker rings, listener grid, ✋ queue, host controls, "● REC" |

**Create→join flow (single host):**
```
1. Host  POST /spaces/create {title, slug, mode}
         → derive_space_id → create LiveKit room → host token (host grant)
2. Member POST /spaces/{id}/join          → FQID verified → listener token
   Guest  GET  /join-space/{id}?invite=…  → guest.py verify → listener token
3. Client connects to the SFU (tailnet) with its token; listeners subscribe only.
4. Listener POST /spaces/{id}/raise-hand  → metadata.hand_raised=true
   Host     POST /spaces/{id}/invite {identity} → metadata.invited_to_stage=true
            → both set → update_participant canPublish=true → they're a speaker
5. Host     POST /spaces/{id}/end → room delete; registry marks ended.
```

**Active-speaker ring** from `ActiveSpeakersChanged`; styled per the 2027 system
(teal accent pulse on the talking speaker).

## 7. Federation (built, not parked) — the MSC4195 model

The only real-world federated-LiveKit system (Matrix's Element Call / MSC4195)
ships **federated identity + federated discovery + single SFU per room**. Media
federation (SFU↔SFU cascading) is unsolved in production (Matrix's Waterfall
stalled), so it stays parked (§9, S6). We build the shipping model:

**`sk-lk-authd` — per-host authorization service (the lk-jwt-service analog):**
- Co-located with each sovereign host's LiveKit SFU; shares the SFU's
  `LIVEKIT_KEY/SECRET` (the SFU never does identity logic — clean seam).
- Verifies a **capauth-signed FQID assertion** (the OpenID-token analog), signed by
  the caller's *home* host identity key (per skcomms PR #5 per-agent key).
- Applies **trust-graph policy** (the `LIVEKIT_FULL_ACCESS_HOMESERVERS` analog,
  but cryptographic): which sovereign hosts/FQIDs get publish vs subscribe-only.
- **Mints the LiveKit JWT** with the local SFU's secret; identity = verified FQID,
  `room` = space_id, grants per role + policy. SFU admits on signature alone.

**Discovery + membership over Nostr** (NIP-53 patterns — federates discovery, not
media):
- **Focus descriptor** (per host): a signed event `{type:"livekit", auth_url,
  sfu_ws_url, host_fqid}` — the `rtc_foci` analog. Clients discover foci via relays.
- **Space state** `kind:30312`-style: a signed event for the room (title, status,
  service URL). **Presence** `kind:10312`-style with a `hand` flag = the federated
  raise-hand. Roles carried as signed `p`-tags with a **proof** (host grants +
  speaker accepts), so roles can't be forged.
- **Focus selection = deterministic `oldest_membership`:** the first valid joiner's
  preferred host pins the Space's SFU; every federated peer computes the same
  answer from replicated signed events. No election RPC.

**Federated join flow:**
```
1. Discover: client queries Nostr relays → signed focus descriptors + Space state
2. Membership: client publishes a signed (FQID) membership event incl. foci_preferred
3. Select: all peers compute oldest valid membership → the winning host's SFU
4. Mint:  client → POST winning_host/sk-lk-authd/get { capauth_fqid_assertion, space_id }
5. Verify+authz: sk-lk-authd checks the signature vs FQID directory + trust-graph
6. Issue: { sfu_ws_url (tailnet), livekit_jwt (local SFU secret, identity=FQID, room, grants) }
7. Connect: client → that one SFU over Tailscale.
```

**Two explicit trust boundaries:** (a) SFU↔authd = shared secret, same host, SFU
does no FQID logic; (b) authd↔remote host = capauth signature + FQID directory +
trust graph — this is where sovereignty lives (the host *chooses* which FQIDs it
admits, cryptographically).

`sk-lk-authd` lives in `skchat` (or a thin `skcomms` service); the signed-event
discovery lands in `skcomms` (extends the `nostr` transport + a Space-event codec).

## 8. Moderation, recording & safety

- **Listeners join muted** — unmute is a *capability* (`canPublish` via the host),
  enforced at the SFU, never trusted to the client.
- **Mute / kick / remove-speaker** = `update_participant` (demote) or token
  revoke (evict). Host-only (`roomAdmin`).
- **Block = routing-suppression, not venue-denial** (explicitly avoid the
  Clubhouse anti-pattern where block denied room entry and was weaponizable).
- **Report** = the primary live-audio moderation lever (you can't pre-moderate
  live speech) → user-flag escalation to the host + audit log.
- **Recording**: **off by default**; Egress room-composite `audio_only` → OGG/MP4.
  A **persistent visible "● REC" indicator** shown to everyone (the established
  consent signal). **Per-speaker consent**: a speaker's track only enters a
  recording after a capauth-signed consent (don't let host intent alone govern a
  speaker's voice). Replays served via the existing recordings UI.

## 9. Phasing

| Phase | Deliverable |
|---|---|
| **S1** | Single-host Space core: `space/roles/tokens/routes`, host opens, members + guest-link listeners join, listen-only works on one tailnet SFU. |
| **S2** | Moderation + **raise-hand mutual-consent** promote/demote + active-speaker ring + mute/kick (`moderation.py`). |
| **S3** | **Recording/replay** (audio-only egress + consent + "● REC") + the local "live now" **registry/directory**. |
| **S4** | **Flutter** Space UI in the consolidated app (the same surface guests get on web). |
| **S5** | **Federation**: `sk-lk-authd` + signed-Nostr focus/Space/membership events + deterministic `oldest_membership` selection + trust-graph policy. (Built — Chef: "do both".) |
| **S6 (parked)** | Big-scale: **HLS-egress fan-out tier** for >~few-thousand passive listeners; SFU↔SFU cascading if/when MSC3898/Waterfall matures. |

## 10. Open items folded into the plan (not blockers)

- Pin the exact `livekit-client`/`livekit-server-sdk` permission-changed event name
  against the installed SDK version (docs vary `ParticipantPermissionsChanged` vs
  singular) — resolved in S2 task 1.
- Space-id derivation collision bound (reuse `derive_room`'s 80-bit base32 with a
  host-FQID + slug input) — confirmed in S1.
- Nostr event kinds: use NIP-53 (`30312` room / `10312` presence+hand) shapes or
  SK-custom kinds — decided in S5 task 1 (lean NIP-53 for interop).
- coturn/funnel exposure for pure public guests beyond the tailnet — S1 reuses the
  existing guest/funnel path; public-scale exposure gated behind the egress tier (S6).
