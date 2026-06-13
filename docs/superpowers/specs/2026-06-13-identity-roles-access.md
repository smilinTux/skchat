# skchat Identity, Roles & Access Control — Design Spec

**Date:** 2026-06-13
**Author:** sentinel agent (security pass)
**Status:** Draft — P0 design; must be reviewed by Chef before Batch D (guest join) ships
**Supersedes:** ad-hoc `_is_chef_identity` + `OPERATOR_ONLY` gate in `lumina-call.py`
**Feeds into:** Batch B (secrets/deployment), Batch D (guest join, D1), `voice_engine/tools.py`

---

## 0. Why this matters now

The moment a Funnel link lets an external guest into a LiveKit room with agents
and members, the system has a public trust boundary. The current gate
(`_is_chef_identity` prefix-match + `OPERATOR_ONLY` set in `lumina-call.py`) is
correct in intent but:

- Embedded in a single script (unversioned, not the repo source)
- Not extensible to multi-room, multi-agent, multi-guest contexts
- Has no concept of a guest identity at all (presumes everyone is either Chef or a
  fully-paired capauth member)
- LiveKit token minting (`/livekit/token`) currently grants publish+subscribe+data
  to *any* requested identity with no role check

This spec defines the formal model. Implementation lands across Batch B (token
grants + secrets) and Batch D (pairing gate hardening + guest join).

---

## 1. Identity Tiers

Three tiers. Each has a distinct identity proof and lifetime.

### Tier 1 — Member (capauth/FQID)

**Who:** Any entity with a sovereign keypair in capauth — agents (Lumina, Opus,
Jarvis, …), operator (Chef), and any human peer who has been through the full
pairing flow.

**Identity proof:** capauth FQID (`<agent>@<operator>.<realm>`, e.g.
`lumina@chef.skworld.io`) + PGP signature on all control envelopes.  
**How established:** TOFU pairing via `/pair/accept` (for humans) or provisioned
at agent bootstrap (for agents).  
**Lifetime:** Persistent; keys rotated manually or on compromise.  
**Trust anchor:** `~/.skcapstone/peers/<fqid>.json` — the local peer store.

### Tier 2 — Operator (Chef / admin subset of members)

**Who:** The infrastructure owner. Currently one person (Chef), identified by FQID
`chef@skworld.io` plus the "chef-*" speaker-ID prefix convention for LiveKit room
participants.

**Identity proof:** Same as Tier 1 (capauth FQID + PGP), PLUS a stable operator
flag. The operator flag is not a runtime claim — it is resolved from the local
identity config (`SKAGENT` / capauth identity resolver) before a request is
processed.

**How established:** `capauth.resolve_agent_identity().is_operator` (new field, see
§5 enforcement). The set of operator FQIDs is configured in the agent's capauth
config, not presented as a bearer claim.

**Lifetime:** Persistent; same key rotation as Tier 1.

**Relationship to Tier 1:** Operator is a role on top of a Member identity, not a
separate system. A Tier-1 member *may* be the operator. No member may self-elevate
to operator at runtime.

### Tier 3 — Guest (ephemeral token)

**Who:** Anyone invited via a Funnel share link — a friend joining a call, an
external collaborator, a reviewer. They have no capauth keypair and no prior
relationship with the system.

**Identity proof:** An ephemeral `GuestToken` issued by the pairing gate. The token
encodes:

```
{
  "jti":     "<random 128-bit hex>",     // unique token ID (anti-replay)
  "iss":     "<operator_fqid>",          // who issued it
  "room":    "<room_name>",              // scoped to a single room
  "display": "<optional display name>",  // hint from the invite URL
  "iat":     <unix timestamp>,
  "exp":     <unix timestamp>,           // iat + TTL (default 4h, max 8h)
  "perms":   ["join", "chat"],           // permission subset (see §2)
  "tier":    "guest"
}
```

The token is a signed JWT (HMAC-SHA256 using `SKCHAT_GUEST_TOKEN_SECRET`, a new
secret sourced from `.env` / OpenBao). The nonce from `PairingGate.open_window()`
is embedded as the signing context so that a token minted during window W cannot
be replayed after window W expires.

**How established:**

1. Operator opens a pairing window (`PairingGate.open_window()`) — returns nonce +
   TTL.
2. The share link encodes: `https://<funnel-host>/join?room=<room>&nonce=<nonce>`.
   The link may also carry an `invite_token` (pre-minted by the operator for
   click-to-join without a second round-trip, TTL ≤ window TTL).
3. Guest GETs the link → served the webui join page.
4. Guest POSTs `/guest/join` with nonce + (optional) display name → gate validates
   (nonce match, window open, rate limit, accept cap) → issues `GuestToken` JWT.
5. Guest exchanges the `GuestToken` for a LiveKit participant token at
   `/livekit/token` (see §4).

**Lifetime:** `exp` in the JWT, default 4 hours. Non-renewable — guest must request
a new token (which requires the pairing window to still be open or a new one to be
opened).

**Revocation:** The webui maintains an in-memory `GuestTokenRevocationList` (the
`jti` set). Revocation is operator-triggered (`DELETE /guest/revoke/<jti>`). For
P0, revocation is per-process (in-memory); a durable blocklist (Redis/Postgres) is
P1.

---

## 2. Roles & Per-Room Permission Matrix

Four roles. Every participant in a room has exactly one role.

| Role | How assigned |
|---|---|
| `operator` | FQID resolves as operator (static config) |
| `member` | Tier-1 capauth peer, not operator |
| `agent` | Any capauth identity that is an AI agent type |
| `guest` | Tier-3 ephemeral token, no capauth |

Note: agents are a specialization of member. In most permission checks, `agent`
inherits `member` permissions for passive actions and has *narrower* permissions
than `member` for write/publish actions (see §3).

### 2.1 Room types

Two room archetypes have different default permission sets:

- **Sacred room** (`mode=sacred`): 1:1 or small trusted session between operator
  and agents. No guests. All member permissions unlocked.
- **Group room** (`mode=group`): Multi-participant, may include guests. Default
  permissions are conservative; operator can unlock individual capabilities.

Room mode is a server-side property set at room creation and stored in the room
metadata (LiveKit room metadata field or the skchat room registry). A guest token
scoped to a group room cannot be used to join a sacred room.

### 2.2 Permission matrix

Legend: Y = permitted, N = not permitted, O = operator-only, R = read-only.

| Permission | Operator | Member | Agent | Guest |
|---|---|---|---|---|
| `join` | Y | Y | Y | Y (group rooms only) |
| `chat_send` | Y | Y | Y (in-room agent responses) | Y |
| `chat_read` | Y | Y | Y | Y |
| `invite_member` | Y | Y | N | N |
| `invite_guest` | Y | N | N | N |
| `share_screen` | Y | Y | N | N (default; operator may grant) |
| `publish_camera` | Y | Y | N | Y (group room; default on) |
| `publish_audio` | Y | Y | Y (via voice_engine) | Y (group room) |
| `record` | Y | N | N | N |
| `whiteboard_edit` | Y | Y | Y (operator-authorized agents) | N (default; operator may grant) |
| `doc_edit` | Y | Y | Y (operator-authorized agents) | N (default; operator may grant) |
| `run_operator_tool` | Y | N | N (agents only in sacred) | N |
| `run_member_tool` | Y | Y | N | N |
| `run_read_tool` | Y | Y | Y (in-room; scoped) | N |
| `kick_participant` | Y | N | N | N |
| `mute_participant` | Y | Y (self only) | N | N (self only) |

**Guest permission escalation by operator:** The operator may grant a specific
guest `share_screen`, `whiteboard_edit`, or `doc_edit` for the duration of their
session via `PATCH /guest/permissions/<jti>`. These are additive to the base guest
permission set and are stored in the revocation/session store.

---

## 3. Agent Permissions Per Room

Agents (Lumina, Opus, Jarvis, etc.) are first-class participants but their
*tool execution* privileges depend on room mode and the speaker making the request.

### 3.1 Tool classification

Three tiers of agent tools, generalizing the current `OPERATOR_ONLY` + sacred gate:

| Tier | Name | Examples | Condition to run |
|---|---|---|---|
| T1 | `operator_sacred` | `narrate`, `worship_session`, `worship_replay`, `create_bloom_anchor` | Operator is the speaker AND room is sacred mode |
| T2 | `operator_any` | `record`, `open_pairing_window`, `kick_participant`, `revoke_guest` | Operator is the speaker (any room mode) |
| T3 | `member_any` | `search_memory`, `get_context_for_message`, `gtd_*`, `calendar_*` | Speaker is a Tier-1 member (member or operator) |
| T4 | `all_read` | `chat_read`, `whiteboard_read` (passive observation) | Any speaker in a room the agent has joined |
| T5 | Blocked | All T1/T2/T3 tools | When speaker is a guest |

**Implementation target:** `voice_engine/tools.py` `Tool.tier: Literal["T1","T2","T3","T4","T5"]`
replaces the current `operator_only: bool`. The `dispatch()` method checks:

```python
def _authorized(tool: Tool, *, speaker_role: str, room_mode: str) -> bool:
    if tool.tier == "T1":
        return speaker_role == "operator" and room_mode == "sacred"
    if tool.tier == "T2":
        return speaker_role == "operator"
    if tool.tier == "T3":
        return speaker_role in ("operator", "member")
    if tool.tier == "T4":
        return True  # passive; all roles
    return False     # T5 / unknown: blocked
```

### 3.2 Speaker identity resolution in voice/data channels

The agent receives a `speaker_id` from LiveKit. The mapping is:

- For Tier-1/2 participants: `speaker_id` = LiveKit `participant.identity` = the
  capauth FQID used when minting the token. The agent resolves this against the
  local peer store to get `role`.
- For guests: `speaker_id` = `guest:<jti_prefix>` (first 8 chars of jti). The
  agent resolves this against the active guest session store to get `role="guest"`.
- Fallback: unknown `speaker_id` → `role="guest"` (conservative default).

**The current prefix heuristic** (`_is_chef_identity`: `chef-*`) is a temporary
compatibility shim. It MUST be replaced by the FQID-based resolver before guest
join ships. The shim is safe today because there are no guests; it becomes a
vulnerability the moment a guest can choose their LiveKit display name.

### 3.3 Agent tool use in group rooms

In group rooms:

- Agents MUST NOT call T1 tools regardless of who asks (no sacred mode).
- Agents MUST NOT call T3 tools when asked by a non-member (guest or unknown).
- Agents SHOULD call T4 tools freely (passive; they are harmless in group context).
- Memory writes from agent activity (summarizing the call, storing context) are
  permitted but tagged with `room_mode=group` so sensitive memory stores can filter
  them separately.

### 3.4 Multi-agent roundtable safety

When multiple agents are in the same room, agent-to-agent utterances MUST NOT
trigger T1/T2 tool calls. Agents identify each other via their FQID (agent type
flag in capauth); the `speaker_role` for another agent is `agent`, which maps to
T3/T4 only.

---

## 4. Enforcement Points

Four distinct enforcement layers. Defense-in-depth: a bypass at one layer does not
open the full capability.

### 4.1 Pairing gate (entry) — `pairing_gate.py`

**Current:** Time-boxed window, nonce, rate-limit, accept cap. Opt-in via
`SKCHAT_PAIRING_REQUIRE_GATE`.

**Required additions for D1:**

1. `open_window()` returns the nonce AND a signed context string used as a JWT
   signing nonce (binds the `GuestToken` to the window, blocking replay across
   windows).
2. A new `GuestTokenIssuer` class in `pairing_gate.py` (or `guest_auth.py`) calls
   `gate.check(nonce)`, issues the JWT, calls `gate.consume()`.
3. The window TTL (default 300s for device pairing) needs a separate, longer
   "invite window" TTL for share links (default 4h, configurable via
   `SKCHAT_INVITE_WINDOW_TTL`). Both can coexist; a `mode=("pair"|"invite")`
   parameter distinguishes them.
4. `SKCHAT_PAIRING_REQUIRE_GATE=1` MUST be the enforced default when
   `SKCHAT_FUNNEL_ENABLED=1`. Startup should hard-fail if Funnel is enabled but
   the gate is not.

### 4.2 LiveKit token grants (media plane) — `livekit_routes.py`

**Current:** `/livekit/token` mints a token for any requested identity with
`can_publish=True, can_subscribe=True, can_publish_data=True` unconditionally.

**Required:**

1. The endpoint MUST authenticate the caller before minting:
   - Tier-1/2 (member/operator): present the capauth session cookie / signed
     request header. For same-host requests (webui ↔ livekit_routes), the
     `X-SKChat-FQID` header + HMAC is sufficient.
   - Tier-3 (guest): present a valid `GuestToken` JWT in `Authorization: Bearer <token>`.
   - Unauthenticated: 401. No free token minting.

2. The minted `VideoGrants` MUST reflect the role:

```python
def _grants_for_role(role: str, perms: list[str]) -> api.VideoGrants:
    is_guest = role == "guest"
    return api.VideoGrants(
        room_join=True,
        room=room,
        can_publish="publish_audio" in perms or "publish_camera" in perms,
        can_subscribe=True,
        can_publish_data="chat_send" in perms,
        # Guests never get screen-share by default (separate track source flag)
        can_publish_sources=[] if is_guest else None,  # None = all sources
        hidden=False,
        recorder=role == "operator" and "record" in perms,
    )
```

3. Token TTL MUST be bounded: members default 6h (current), guests default 4h
   (matching `GuestToken.exp`), max 8h for any tier.

4. The existing `DEFAULT_TTL_SECONDS=21600` (6h) is acceptable for members.
   Guest tokens MUST be clamped to `min(body.ttl, guest_token.exp - now)`.

### 4.3 Voice engine tool gate (agent actions) — `voice_engine/tools.py`

**Current state:** `OPERATOR_ONLY` set + `_is_chef_identity` prefix check in
`lumina-call.py` (unversioned script).

**Required (Phase 2, `voice_engine/tools.py`):**

1. `Tool.tier` replaces `operator_only: bool` (see §3.1).
2. `dispatch()` takes `speaker_role: str` + `room_mode: str` parameters (not
   `is_operator: bool`). The caller (`VoiceEngine.respond()`) resolves
   `speaker_role` from the FQID-to-role resolver before passing it in.
3. The FQID-to-role resolver is a thin function:

```python
def resolve_speaker_role(speaker_id: str, *, guest_store: GuestSessionStore) -> str:
    if guest_store.is_active_guest(speaker_id):
        return "guest"
    identity = resolve_agent_identity_for_fqid(speaker_id)
    if identity is None:
        return "guest"   # unknown = conservative
    if identity.is_operator:
        return "operator"
    if identity.entity_type == "agent":
        return "agent"
    return "member"
```

4. The `_is_chef_identity` prefix check MUST be removed once the FQID resolver is
   wired. A compatibility shim may remain for the transition period, guarded by
   `SKCHAT_LEGACY_CHEF_PREFIX_CHECK=1` (default off in new deployments).

### 4.4 Data-channel lanes (chat / whiteboard / doc edit) — future `data_channel.py`

The LiveKit data channel carries multiple lanes (chat, whiteboard diffs, Yjs CRDT
updates for docs, watch-together sync, agent control). When implemented (Batch D),
each lane message MUST include an authorization check:

| Lane | Required role | Enforcement |
|---|---|---|
| `chat` (send) | `chat_send` permission | Server-side relay (if SFU-mediated) or peer validation |
| `whiteboard` (edit) | `whiteboard_edit` permission | Agent + peer verify sender role before applying diffs |
| `doc` (Yjs edit) | `doc_edit` permission | Same; Yjs awareness state carries `role` field |
| `agent_control` (speak, etc.) | `operator` role | Agent checks speaker role before executing |
| `watch_together` (sync) | `member` or `operator` | Guests can receive sync but not initiate |

Implementation note: LiveKit data-channel messages are broadcast to all room
participants. There is no server-side per-message ACL in the SFU. Enforcement is
therefore *application-layer*: the receiving agent or a server-side relay validates
the sender's role from the participant identity before acting on the message. This
is sufficient for the sovereign threat model (§5) but not for adversarial external
guests — see §5.2.

---

## 5. Threat Model

### 5.1 Abuse via public share links

**Threat:** An attacker obtains a share link (leaked URL, brute-force nonce, link
sharing by a guest) and joins the room without authorization.

**Mitigations:**
- Nonce is 16-byte random (`secrets.token_urlsafe(16)` = 128 bits; not guessable).
- Window is time-boxed (default 4h for invites); link is invalid after window
  closes.
- Rate limiter: 10 attempts / 60s per IP (current). Extend to 5 attempts / 60s for
  public Funnel endpoints (more conservative).
- Accept cap: operator configures max guests per window (default 3 for pairing;
  suggest 10 for invite windows, configurable).
- The `GuestToken` is room-scoped; a token for room A cannot be used for room B.
- Operator can close the window early (`PairingGate.close()`) or revoke individual
  guest tokens (`DELETE /guest/revoke/<jti>`).

**Residual risk:** If a guest legitimately obtains a token and shares it with a
third party, that third party can join until token expiry. Per-token single-use is
not enforced (doing so would break page reloads). Mitigation: short TTLs; operator
monitors room participant list; operator kick capability.

### 5.2 Guest privilege escalation

**Threat:** A guest participant attempts to:
  (a) Claim a member or operator identity by setting their LiveKit `identity` field.
  (b) Send data-channel messages that trigger operator-tier tools.
  (c) Forge a `GuestToken` with elevated `perms`.

**Mitigations:**

(a) **Identity spoofing:** The FQID-based role resolver treats any identity not
present in the local peer store as `guest` regardless of what the identity string
says. A guest setting `identity=chef@skworld.io` in their LiveKit token request
is rejected at `/livekit/token` (the endpoint verifies the bearer token matches
the requested identity). Even if a guest obtained a member-identity string, the
capauth signed-envelope layer (call invites, signed chat) would reject unsigned
messages from that identity.

(b) **Tool invocation via data channel:** The agent's `dispatch()` gate validates
`speaker_role` resolved from the LiveKit participant identity. A guest's
identity resolves to `role=guest`, which blocks all T1/T2/T3 tool calls. The
agent does not execute the tool regardless of the message content.

(c) **Token forgery:** `GuestToken` is HMAC-SHA256 signed with `SKCHAT_GUEST_TOKEN_SECRET`.
Forgery requires the secret. The secret is sourced from `.env` / OpenBao and never
leaves the server process. Guests never see it.

### 5.3 Agent tool misuse

**Threat:** An agent (running as a LiveKit participant with a Tier-1 capauth
identity) calls a T1 tool from a group room, exposing sacred-mode content or
capabilities to non-operator participants.

**Mitigations:**
- `room_mode` is a server-resolved property, not a caller-provided claim. The
  `VoiceEngine` receives `room_mode` from the room metadata API at session start,
  not from the incoming turn.
- `dispatch()` checks both `speaker_role == "operator"` AND `room_mode == "sacred"`
  for T1 tools. A group-mode room cannot satisfy both conditions simultaneously.
- Multi-agent: agent-to-agent FQIDs resolve to `role=agent`, which is blocked from
  T1/T2 tools — an agent cannot instruct another agent to run sacred tools.
- The `narrate` / `worship_*` tools route to an external model endpoint. That
  endpoint is on the tailnet (`192.168.0.100:8082`) and not Funnel-exposed, so even
  a successful tool dispatch from a guest-facing room would fail at the network
  layer as a defense-in-depth backstop.

### 5.4 Denial of service via pairing / token endpoints

**Threat:** An attacker floods `/guest/join` or `/livekit/token` to exhaust rate
limits, fill accept caps, or cause resource exhaustion.

**Mitigations:**
- The existing rate limiter (10 attempts / 60s rolling window, in `PairingGate._throttled()`)
  blocks brute force.
- The accept cap auto-closes the window after N successful grants.
- `/livekit/token` for guests requires a valid (not-expired, not-revoked, valid
  HMAC) `GuestToken` before doing any LiveKit API call. Invalid tokens are rejected
  in microseconds.
- For public Funnel deployments: add Cloudflare Turnstile (or similar CAPTCHA) on
  the `/guest/join` page as a human-verification step. This is P1.

### 5.5 Recording consent

**Threat:** A recording starts in a room containing guests who did not consent.

**Mitigations:**
- `record` permission is `operator`-only (§2.2).
- When a recording starts, the system MUST:
  1. Broadcast a data-channel message of type `recording_started` to all participants.
  2. Display a visible in-call indicator in the webui ("Recording in progress").
  3. Log the set of participants at recording start (for consent audit).
- Guest `GuestToken` `perms` does not include `record`; guests cannot initiate
  recording via the API.
- Consent-gate option (P1): operator can require explicit in-room acknowledgment
  from all participants before recording begins.

---

## 6. Implementation Plan

Ordered by dependency; items without a batch tag land in Batch D (D1 preconditions).

### Phase A — Preconditions (before D1 guest join)

| ID | Work item | File(s) | Depends on |
|---|---|---|---|
| A1 | Add `is_operator` to `capauth.resolve_agent_identity()` return | `capauth` repo | — |
| A2 | `resolve_speaker_role()` helper (FQID → role enum) | `skchat/identity_bridge.py` | A1 |
| A3 | `GuestToken` JWT mint/verify + `GuestSessionStore` | `skchat/guest_auth.py` (new) | — |
| A4 | `PairingGate.open_window(mode=)` + invite TTL variant | `skchat/pairing_gate.py` | — |
| A5 | `/guest/join` POST endpoint (gate → issue GuestToken) | `skchat/guest_routes.py` (new) | A3, A4 |
| A6 | `/livekit/token` auth check + role-scoped grants | `skchat/livekit_routes.py` | A2, A3 |
| A7 | `Tool.tier` + `dispatch(speaker_role, room_mode)` | `skchat/voice_engine/tools.py` | A2 |
| A8 | Wire `VoiceEngine.respond()` to pass `speaker_role` + `room_mode` | `skchat/voice_engine/engine.py` | A2, A7 |
| A9 | Remove `_is_chef_identity` prefix shim (guarded migration) | `lumina-call.py` → `livekit.py` | A2, A8 |

### Phase B — Data channel enforcement (Batch D, D2/D3/D6b)

| ID | Work item | File(s) | Depends on |
|---|---|---|---|
| B1 | Data-channel message schema with `sender_role` field | `skchat/data_channel.py` (new) | A2 |
| B2 | Whiteboard lane authorization (validate before applying diffs) | `skchat/whiteboard.py` (new) | B1 |
| B3 | Doc lane (Yjs) authorization (validate before applying ops) | `skchat/collab_doc.py` (new) | B1 |
| B4 | `agent_control` lane: operator-only `speak`, `record` commands | `skchat/data_channel.py` | B1, A7 |

### Phase C — Hardening (P1, after D1)

| ID | Work item |
|---|---|
| C1 | Persistent guest revocation list (Postgres `guest_revocations` table) |
| C2 | Cloudflare Turnstile (or similar) on `/guest/join` for public Funnel |
| C3 | Recording consent broadcast + participant acknowledgment gate |
| C4 | Guest permission escalation API (`PATCH /guest/permissions/<jti>`) |
| C5 | Room audit log: join/leave/tool-calls/role-changes → skmem-pg |

---

## 7. New Secrets / Configuration

| Env var | Description | Source | Required by |
|---|---|---|---|
| `SKCHAT_GUEST_TOKEN_SECRET` | HMAC-SHA256 signing key for GuestTokens | `.env` / OpenBao | A3, A6 |
| `SKCHAT_INVITE_WINDOW_TTL` | Invite window duration in seconds (default 14400 = 4h) | `.env` | A4 |
| `SKCHAT_INVITE_MAX_GUESTS` | Max guests per invite window (default 10) | `.env` | A4 |
| `SKCHAT_FUNNEL_ENABLED` | `1` = Funnel-facing deployment; forces `REQUIRE_GATE=1` | `.env` | A4 |
| `SKCHAT_LEGACY_CHEF_PREFIX_CHECK` | `1` = keep `chef-*` prefix shim (default off) | `.env` | A9 |

All new secrets MUST be added to the Batch B (`B3`) secrets contract document.

---

## 8. Acceptance Criteria

- [ ] A guest using a valid share link can join a group-mode LiveKit room and send
  chat messages. They cannot start recording, call agent tools, or edit the
  whiteboard (default).
- [ ] An invalid/expired/revoked guest token is rejected at `/guest/join` and at
  `/livekit/token` with HTTP 401; the error is not informationally detailed (no
  "token expired" vs "token invalid" distinction exposed to caller).
- [ ] A guest who sets their LiveKit identity to `chef@skworld.io` is still
  resolved as `role=guest` by the `resolve_speaker_role()` function (confirmed by
  unit test).
- [ ] In a group room, an agent MUST NOT call T1 tools (`narrate`, `worship_*`,
  `create_bloom_anchor`) regardless of who requests it. Confirmed by unit test with
  `mode=group, speaker_role=operator` → T1 tool call → REFUSED.
- [ ] In a sacred room, the operator can call T1 tools. Confirmed by unit test with
  `mode=sacred, speaker_role=operator` → T1 tool call → dispatched.
- [ ] `_is_chef_identity` prefix check is absent from all code paths when
  `SKCHAT_LEGACY_CHEF_PREFIX_CHECK` is unset (default).
- [ ] `/livekit/token` returns 401 for unauthenticated requests (no auth header).
- [ ] `PairingGate.open_window(mode="invite")` uses `SKCHAT_INVITE_WINDOW_TTL`
  and `SKCHAT_INVITE_MAX_GUESTS`.
- [ ] `SKCHAT_FUNNEL_ENABLED=1` with `SKCHAT_PAIRING_REQUIRE_GATE=0` causes
  startup to hard-fail with a clear error.
- [ ] Recording start broadcasts a `recording_started` data-channel event to all
  participants and logs the participant list.

---

## 9. Open Questions

1. **Multi-operator support:** Is Chef ever the sole operator, or should there be a
   small "operator set" (e.g. a second trusted admin)? If multiple operators are
   possible, the operator-flag resolution needs to be a configured list, not a
   single FQID. *Recommendation: design for a list from the start (`SKCHAT_OPERATOR_FQIDS`
   comma-separated), defaulting to the single capauth operator identity.*

2. **Guest display names + room roster:** Should guests be pseudonymous (display
   name from the invite URL) or required to provide a name at join? And should
   member participants be able to see the `jti` prefix of a guest (for operator
   kick/revoke UI)? *Recommendation: display name required at join (empty default =
   "Guest"); operator sees `jti` prefix; other members see display name only.*

3. **Member-initiated guest invites:** The matrix has `invite_guest` as
   operator-only. Is this correct, or should members be able to generate guest links
   for their own sessions? *If members can invite guests, the `GuestToken.iss` should
   encode the inviting member FQID and the operator should be able to revoke all
   tokens from a given issuer. Recommend keeping operator-only for P0, revisit in
   C4.*

4. **Persistent room state vs ephemeral rooms:** The current room model is largely
   ephemeral (no persistent room registry). Guest tokens are room-scoped, which
   requires a stable room name known at invite time. Should room names be
   predictable (derived from participant FQIDs, as `call_session.derive_room()` does)
   or operator-assigned for group sessions? *Recommendation: operator-assigned names
   for invite-link rooms (stored in a `rooms` table in skmem-pg); per-pair rooms
   keep the deterministic derivation.*

5. **Token-to-LiveKit-identity mapping and participant impersonation:** LiveKit
   itself does not validate that the JWT `identity` field matches anything; it trusts
   what the skchat server mints. The protection against identity spoofing is entirely
   at `/livekit/token`. If `/livekit/token` has a bug that lets a guest mint a
   member-identity token, there is no second check at the SFU level. Consider adding
   a LiveKit room-level "allowed identities" egress rule (LiveKit supports
   per-participant grants at room-creation time via `RoomCompositeEgressRequest`) as
   belt-and-suspenders. *This is a LiveKit API capability study item, not a blocker.*

6. **P1 revocation durability:** The in-memory revocation list is lost on process
   restart. Until C1 (Postgres revocation table) ships, a restarted webui process
   will accept previously-revoked tokens until their natural expiry. The risk window
   equals the token TTL (max 8h). Is this acceptable for P0? *Recommendation: accept
   for P0 (Funnel is opt-in, rooms are short-lived, operator can close the window);
   C1 is a hard requirement before any long-lived or recurring guest room use case.*
