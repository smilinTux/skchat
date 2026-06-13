# D1 — One-Link Guest Join

**Date:** 2026-06-13  
**Status:** Scaffolded — server logic + tests done; route wiring TODOs marked  
**Author:** artisan agent  
**Batch:** D (guest UX)  
**Depends on:** `2026-06-13-identity-roles-access.md` (§1 identity tiers, §4 enforcement)  
**New module:** `src/skchat/guest.py`  
**New tests:** `tests/test_guest.py`

---

## 1. Goal

Send a friend a link → they open it in a browser → they join a LiveKit room
alongside Lumina and Opus. No Tailscale, no pairing flow, no account. Their
media travels via coturn (ICE tier 3) when a direct route is unavailable.

---

## 2. Full Flow

```
 Operator                    skchat webui                  Guest browser
    │                              │                              │
    │── POST /guest/invite ─────►  │                              │
    │   {room, display?, ttl?}     │                              │
    │◄── {invite_url} ────────────  │                              │
    │                              │                              │
    │ (shares link out-of-band)    │                              │
    │                              │ ◄── GET /join/<room>?invite= ─│
    │                              │     (Funnel edge)            │
    │                              │ ──► 200 HTML (join page) ───► │
    │                              │                              │
    │                              │ ◄── POST /guest/join ─────── │
    │                              │     {room, invite, display}  │
    │                              │                              │
    │                              │  validate invite JWT         │
    │                              │  mint GuestToken JWT         │
    │                              │  mint LiveKit token          │
    │                              │ ──► {lk_token, lk_url, …} ──► │
    │                              │                              │
    │                              │ ◄── WebSocket (LiveKit SFU) ─ │
    │                              │     audio/video via coturn   │
```

### Step-by-step

1. **Operator creates an invite** — `POST /guest/invite` (operator-only endpoint;
   requires `X-SKChat-FQID` header or session cookie). Returns a signed `invite_token`
   (JWT) and the full `invite_url`:
   `https://<FUNNEL_HOST>/join/<room>?invite=<invite_token>`

2. **Operator shares the URL** out-of-band (Telegram, Signal, copy-paste). The URL
   is the only secret; anyone who has it can join until it expires.

3. **Guest GETs `/join/<room>?invite=…`** — served the guest landing page (HTML).
   The page shows: room name, who will be present (agents), a display-name input,
   and a "Join" button.

4. **Guest POSTs `/guest/join`** — `{room, invite_token, display_name}`. Server:
   - Verifies the `invite_token` JWT (HMAC-SHA256 + expiry + room-scope).
   - Checks the invite is not in the revocation list.
   - Derives a `guest_identity` string: `guest:<jti_prefix8>`.
   - Mints a `GuestToken` scoped to the room + guest identity.
   - Mints a LiveKit participant token from the GuestToken (restricted grants).
   - Returns `{lk_token, lk_url, room, identity, expires_at}`.

5. **Guest connects to LiveKit** — browser receives the token; livekit.html can be
   redirected to with `?room=<room>&identity=<identity>&token=<lk_token>` for
   auto-connect.

6. **Media routing** — LiveKit SFU brokers all tracks. For guests outside the
   tailnet, LiveKit uses TURN (coturn at tier 3) if configured. When `SKCHAT_TURN_URLS`
   and `SKCHAT_TURN_SECRET` are set, ICE tier 3 credentials are embedded in the
   LiveKit server config; no per-guest credential call is needed.

---

## 3. Invite Token Shape

```json
{
  "jti":      "a3f2...64-char hex",
  "iss":      "lumina@chef.skworld.io",
  "room":     "lumina-and-chef",
  "display":  "Alice",
  "iat":      1718300000,
  "exp":      1718314400,
  "tier":     "invite"
}
```

- Signed with `SKCHAT_GUEST_TOKEN_SECRET` using HS256 (PyJWT).
- TTL default 4 h (`SKCHAT_INVITE_WINDOW_TTL`, max 8 h).
- `room` claim binds the token to a single room; cannot be used to join another.
- `jti` is globally unique (128-bit random hex); used for revocation lookups.

---

## 4. GuestToken Shape (internal server object)

`GuestToken` is a dataclass produced by `InviteVerifier.verify()`. It is **not**
sent to the guest. Instead it is used in the same request to build the LiveKit
grants.

```python
@dataclass
class GuestToken:
    jti: str          # from invite_token
    room: str
    identity: str     # "guest:<jti[:8]>"
    display: str      # from POST body or invite hint
    exp: float        # unix timestamp (from invite_token.exp)
    perms: list[str]  # ["join", "chat_send", "publish_audio", "publish_camera"]
```

---

## 5. LiveKit Grant Mapping for Guests

```python
VideoGrants(
    room_join        = True,
    room             = guest_token.room,
    can_publish      = True,          # audio + camera only
    can_subscribe    = True,
    can_publish_data = True,          # chat lane (not agent_control)
    can_publish_sources = [           # explicitly limit track sources
        "camera",
        "microphone",
    ],
    hidden           = False,
    recorder         = False,
)
```

Token TTL is clamped to `min(requested_ttl, invite_token.exp - now())`.

---

## 6. Security

### 6.1 Invite lifecycle

| Event | Effect |
|---|---|
| Token expired (`exp < now`) | `InviteVerifier.verify()` raises `GuestJoinError` |
| Token in revocation list | Same rejection; no distinction exposed to caller |
| Signature tampered | JWT decode raises; caught → `GuestJoinError` |
| Wrong room in body | `GuestJoinError("room mismatch")` → HTTP 401 |
| Rate limit exceeded | HTTP 429 (enforced by `PairingGate` or a simple IP counter) |

### 6.2 Guest cannot escalate

- `identity` for the LiveKit token is always `guest:<jti[:8]>` — the server sets
  it; the guest cannot choose their LiveKit identity.
- The `resolve_speaker_role()` helper (spec §4.3) resolves any `guest:*` prefix to
  `role="guest"`, which blocks all T1/T2/T3 agent tool calls.
- `can_publish_sources` restricts track sources at the SFU level — no screen-share
  by default.

### 6.3 Revocation

`InviteIssuer` and `InviteVerifier` share a module-level `_revoked_jtis: set[str]`.
`revoke_invite(jti)` adds to this set. Revoked tokens are rejected in microseconds
before any LiveKit API call is made. The set is process-lifetime (P0 is acceptable
per spec §5; C1 adds durable Postgres revocation).

### 6.4 Public endpoints

`/guest/invite` MUST NOT be Funnel-exposed. It is an operator-only endpoint (same
tailnet-only access as `/call/start`). Only `/join/<room>` and `/guest/join` need
to be reachable via Funnel.

---

## 7. Guest Landing Page

The guest landing page reuses `livekit.html` with URL parameters — no new HTML
file needed for P0.

**Redirect target after `/guest/join` succeeds:**

```
/livekit/<room>?room=<room>&identity=guest:<jti8>&token=<lk_token>
```

`livekit.html` already handles `?room=`, `?identity=`, and `?token=` query params
(lines 899–915 of `livekit.html`). When all three are present the page
auto-connects without any user input.

**What the guest sees at `/join/<room>`** (served by `_serve_guest_join_page()`):

1. Room name heading: "You're invited to join **lumina-and-chef**"
2. Agent presence list: "Lumina and Opus are in this room"
3. Display name input (required, max 40 chars)
4. "Join" button → JS POSTs to `/guest/join` → redirects to the livekit page
5. On error: inline error message (no detail that distinguishes expired vs invalid)

The join page is minimal HTML (no external deps) served inline from `guest.py` as
a string template. It can be promoted to a `static/guest_join.html` file in a
follow-up.

---

## 8. Route Wiring TODOs

The logic is fully implemented and tested in `guest.py`. The FastAPI routes need
to be wired into the webui app. Two `# TODO wire route` comments mark the spots.

**To wire:**

```python
# In webui.py or a new guest_routes.py, call after livekit_routes:
from skchat.guest import register_guest_routes
register_guest_routes(app)
```

Routes to add:

| Method | Path | Auth | Handler |
|---|---|---|---|
| `POST` | `/guest/invite` | operator (tailnet only) | `InviteIssuer.create_invite()` → JSON |
| `GET`  | `/join/{room}` | public (Funnel) | Serve join HTML page |
| `POST` | `/guest/join` | public (Funnel) | `InviteVerifier.join()` → LiveKit token |
| `DELETE` | `/guest/revoke/{jti}` | operator | `revoke_invite(jti)` |

The `GET /join/{room}` and `POST /guest/join` endpoints are the Funnel-facing
ones. They MUST only be enabled when `SKCHAT_FUNNEL_ENABLED=1` or
`SKCHAT_PAIRING_REQUIRE_GATE=1`.

---

## 9. Configuration

| Env var | Default | Notes |
|---|---|---|
| `SKCHAT_GUEST_TOKEN_SECRET` | *(required)* | HMAC-SHA256 signing key; generate with `openssl rand -hex 32` |
| `SKCHAT_INVITE_WINDOW_TTL` | `14400` (4 h) | Max TTL of an invite link |
| `SKCHAT_FUNNEL_PUBLIC_URL` | — | Base URL for invite links (existing env var) |
| `SKCHAT_FUNNEL_ENABLED` | `0` | When `1`, gate enforcement is mandatory |

---

## 10. Open Items (P1)

- **Durable revocation list** (C1): replace in-memory `_revoked_jtis` with a
  Postgres `guest_revocations` table.
- **Operator kick** (`PATCH /guest/kick/{jti}`): close the LiveKit room participant
  via the LiveKit server API.
- **Join page as static file**: promote the inline HTML template to
  `static/guest_join.html` for easier maintenance.
- **Cloudflare Turnstile** on `/guest/join` as a CAPTCHA gate for public Funnel
  exposure (C2 from identity spec).
- **Persona list in join page**: fetch `/livekit/config` + room participants to
  show which agents are live rather than a static list.
