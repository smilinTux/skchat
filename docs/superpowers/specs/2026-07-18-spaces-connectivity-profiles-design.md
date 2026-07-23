# Spaces Connectivity Profiles (per-user ICE selection)

Status: APPROVED (operator, 2026-07-18). Ready for implementation planning.

## Problem

Spaces media runs over LiveKit (SFU). Today the SFU is tailnet-first: tailnet
clients connect direct over UDP to `100.108.59.57`, and off-tailnet clients relay
through the sovereign coturn (advertised to every client via LiveKit
`rtc.turn_servers`, see the memory note `spaces-turn-coturn-2026-07-17`). That is
fully sovereign: no third party ever sees media or metadata.

The operator wants an OPT-IN path that improves connectivity and scale for external
clients by leveraging public ICE, while acknowledging it is less sovereign and
letting each user choose their own posture.

## The key distinction (drives the whole design)

"Public ICE servers" is two very different things:

- **Public STUN** (Google, Cloudflare): the client asks "what is my public IP?" to
  form a server-reflexive candidate that traverses NAT. Media NEVER touches the STUN
  server; it learns only the client's public IP, once. Low privacy cost, large
  connectivity/scale benefit (more clients connect direct instead of relaying).
- **Public TURN** (Twilio, metered): a third party RELAYS the actual audio/video.
  Real sovereignty cost. Out of scope: the sovereign coturn already covers the
  restrictive-NAT cases that need a relay.

Decision: offer **public STUN assist only**. The sovereign coturn stays the sole
relay in every profile. We never route media through a party we do not control.

## Design

### Phase 1: per-user Connectivity profile (the feature)

Two profiles, default is today's behavior:

| Profile | ICE the client uses | Who sees what |
|---|---|---|
| **Sovereign** (default) | tailnet host UDP (on tailnet) or coturn TURN relay (off) | nobody outside our infra |
| **Balanced** (opt-in) | the above PLUS public STUN (Google + Cloudflare) for a srflx candidate | the STUN server learns THIS user's public IP; media still only ever relays through our coturn |

- **Per-user, client-side.** STUN's only cost is exposing the choosing user's own
  public IP, so the choice is theirs alone and must be applied per client, not
  globally. Global `rtc.stun_servers` would expose everyone; rejected.
- **Storage.** Web: `localStorage` (via the existing `safeStorage` helpers). App:
  the Flutter settings store.
- **Application.** On connecting to a Space, the client builds its ICE set from the
  chosen profile and passes it to LiveKit's connect config (web:
  `LK.Room({ rtcConfig: { iceServers } })`; app: `RoomOptions`/`ConnectOptions`
  rtcConfiguration). CAVEAT to verify in the plan: whether LiveKit MERGES
  client-supplied `iceServers` with the server-delivered coturn or REPLACES them. If
  it replaces, the client set must re-include the coturn relay so the sovereign
  fallback is never dropped. The Sovereign profile may simply pass nothing and use
  the server-delivered set unchanged.
- **Acknowledgment.** Flipping to Balanced shows a one-time, plain-language notice:
  "This lets your device use a public STUN server (Google/Cloudflare) to connect
  more directly. It reveals your public IP to that server. Your audio and video
  still only ever relay through our own server." Default stays Sovereign.
- **Default off, honest copy, reversible per user.** No behavior change for anyone
  who does nothing.

### Phase 2: SFU public UDP path (what makes STUN actually pay off)

Client-side STUN alone changes nothing until the SFU also has a publicly reachable
candidate. Today the SFU advertises only its tailnet address, so even a Balanced
client still relays through coturn.

- Forward the SFU UDP range (`50000-50200`, plus `7880`/`7881` as needed) on the
  router to `.158`; set the SFU `external_ip`/`use_external_ip` and its own
  `stun_servers` so it advertises a reachable public candidate.
- Then a Balanced client and the SFU can form a direct UDP pair; LiveKit ICE prefers
  it over the relay automatically, and coturn drops back to a true fallback. This is
  the "do not relay everyone at scale" payoff the operator asked for.
- Phase 2 needs a router change (operator-owned) and is tracked separately in GTD.
  Phase 1 ships the user-choice surface and is inert-safe without Phase 2.

## Sovereignty note (honest-claim gate, per sk-standards)

- Sovereign profile: zero third-party exposure (unchanged default).
- Balanced profile: the choosing user's public IP is disclosed to a public STUN
  provider. Media and all signaling stay on sovereign infra. This is an explicit,
  per-user, acknowledged trade, never silent, never global.
- We never add public TURN; media never transits a third party.

## Testing

- Web: markup/unit assertions that the profile setting persists, that Sovereign
  passes no public ICE, that Balanced adds exactly the two public STUN URLs plus the
  coturn relay, and that the acknowledgment gates the switch.
- App: unit test the profile-to-iceServers builder for both profiles; widget test
  the setting toggle + acknowledgment.
- Verify (documented, manual): LiveKit merge-vs-replace behavior for client
  `iceServers`, so the coturn relay is provably retained under Balanced.

## Out of scope

Public TURN of any kind. Global STUN. Phase 2 router work (separate GTD item).

## Surfaces touched (for the plan)

- Web `src/skchat/static/space.html`: settings UI + ICE builder + persistence.
- Flutter `lib/features/spaces/` + a settings store: setting + ICE builder + connect
  wiring; likely shared with the 1:1/conf connect path.
- No server route changes (the coturn baseline stays server-delivered).
