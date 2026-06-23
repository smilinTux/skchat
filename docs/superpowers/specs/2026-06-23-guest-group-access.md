# Guest Access via Shareable Links — group-scoped, full-in-room, untrusted

Spec · 2026-06-23 · branches `feat/guest-group-access` (skchat + skchat-app)

## Goal

Operator texts a link. A recipient opens it, types a name, and joins **ONE
specific group** as an **UNTRUSTED guest** with **FULL in-room functionality**
(text, video/voice call, screenshare, file share+download, reactions/replies/
threads) — but **no admin/expansion powers** (cannot invite, create rooms, mint
links, see any OTHER conversation/file/peer, use agent tools, or do group-admin
actions).

This is distinct from the existing call-only guest path (`guest.py`,
`join_routes.py`, `conf/routes.py`) which mints LiveKit-only invites for conf
rooms. Guest *group* access reuses that machinery for the call leg but adds a
**group-scoped chat/file session** on top.

## Feature flag

All guest-group routes are gated behind `SKCHAT_GUEST_LINKS_ENABLED`
(default **off**). When off, every route returns 404 (operator routes) / 403
(guest routes). No public ingress is wired — tested on the private tailnet URL
first. Public exposure is a later operator decision (deferred).

## Identity model

- Guest types a **name** (minimum identity).
- First open: the **browser generates an ephemeral keypair** (WebCrypto:
  ECDSA P-256, simpler than ed25519 in browsers) stored in `localStorage`, so
  the same link → same guest next time (no re-setup).
- Identity = `guest:<slug(name)>#<pubkey-fp>` where `<pubkey-fp>` is the first
  16 hex chars of SHA-256 over the exported SPKI public key.
- **UNTRUSTED** — self-asserted, never capauth-verified. Rendered with an
  untrusted badge + low trust level. Added to the group as an `observer`-typed
  **untrusted member** (`participant_type=human`, `metadata.guest=true`,
  `trust="untrusted"`), but with *posting* allowed (the ACL read-only gate is
  bypassed only insofar as a normal member would post — see capability matrix).
- Guests **sign their messages** with the browser key (detached ECDSA over the
  canonical `{group_id, body, ts}` JSON). The server records the signature +
  pubkey on the message metadata so the UI can render trusted/untrusted state.
  The signature is **advisory** (we do not hold a capauth-proven binding); it
  proves *same-browser continuity*, not real-world identity.

## Token model

Two tokens:

1. **Invite token** (operator → recipient, the link secret). HS256 JWT signed
   with `SKCHAT_GUEST_TOKEN_SECRET` (reused from `guest.py`). Claims:
   `{jti, tier:"group-invite", group_id, iat, exp, once?}`. Room-scoped to the
   group (`group_id`); revocable (reuses `guest.revoke_invite` JTI store);
   optional expiry/single-use. `join_url` is relative: `/join/<token>`.

2. **Guest session token** (server → guest browser). HS256 JWT signed with the
   same secret. Claims:
   `{jti, tier:"guest-session", group_id, guest_id, name, fp, iat, exp}`.
   **Scoped to exactly ONE `group_id`.** Carried by the guest browser as a
   bearer token (`Authorization: Bearer <session>` / `X-Guest-Token`) on every
   guest API call. The server decodes it, pins the request to its `group_id`,
   and rejects (403) any access to a different group/conversation/file/peer or
   any invite/create/admin/agent-tool action.

3. **LiveKit guest call token** — minted via the existing
   `daemon_proxy_groupcall.mint_member_token` / `guest.build_livekit_token`
   path: publish audio/video/**screen** + subscribe, **never** room_admin.
   Room = `derive_group_room(group_id)` (same room real members join).

## Guest capability grants (what the session token authorizes)

| Capability (scoped to the invited group_id ONLY) | Allowed |
|---|---|
| Read the group conversation | ✅ |
| Send text messages (signed) | ✅ |
| Reactions / replies / threads | ✅ |
| Send + receive (download) files in the chat | ✅ |
| Join the group call (audio/video) | ✅ |
| Screenshare in the call | ✅ |
| Invite / add members / mint links | ❌ 403 |
| Create other rooms / groups | ❌ 403 |
| See/access any OTHER conversation/room/file/peer | ❌ 403 |
| Use agent tools | ❌ 403 (no agent/MCP surface exposed to guests) |
| Group-admin (rename, remove members, change ACL) | ❌ 403 |

## One-room isolation + no-invite/no-create — server-side enforcement

- Every guest route takes the session token, decodes it, and derives the bound
  `group_id` **from the token** — never from a caller-supplied path/body group
  id. A request whose path/body group id ≠ token group id → **403**.
- Guest routes are a **separate, narrow router** (`/api/v1/guest/*`). The
  operator proxy (`/api/v1/*` in `daemon_proxy.py`) is unchanged and stays
  operator-implicit; guests never reach it (no guest token is accepted there).
- There is **no** guest endpoint for: listing conversations/peers, creating
  groups, adding members, minting invites, group update, agent tools. The
  capability surface is exactly: send / history / react / edit-own / file
  upload+download (scoped) / call token. Anything else simply does not exist
  for a guest, and the few shared file/download routes verify the transfer
  belongs to the bound group before serving bytes.
- File scoping: a guest upload is fanned into the bound group only; a guest
  download is allowed only for a `transfer_id` recorded as belonging to the
  bound group (a per-group transfer allowlist in the guest store).

## Routes (skchat)

Operator (capauth/operator-gated, reuse `guest._require_operator`):
- `POST   /api/v1/groups/{id}/invite` → mint room-scoped invite → `{token, join_url}`
- `DELETE /api/v1/groups/{id}/invite/{token}` → revoke (by jti embedded in token)
- `GET    /api/v1/groups/{id}/invites` → list active (best-effort)

Guest (session-token-gated, flag-gated):
- `POST   /api/v1/guest/join {invite_token, display_name, guest_pubkey}` →
  validate invite → create/lookup untrusted guest, add to group → return guest
  session token + LiveKit guest call token + group bootstrap.
- `GET    /api/v1/guest/conversation` → the bound group thread (token-scoped).
- `POST   /api/v1/guest/send {body, reply_to_id?, ts, signature}` → post signed.
- `POST   /api/v1/guest/react {message_id, emoji, op}`
- `POST   /api/v1/guest/file` (multipart) → upload into the bound group.
- `GET    /api/v1/guest/file/{transfer_id}` → download (group-scoped allow).
- `POST   /api/v1/guest/call` → LiveKit guest token (publish A/V/screen).

Landing (flag-gated, public-of-tailnet HTML):
- `GET /join/{token}` → already exists for conf; we keep the SPA route in the
  Flutter app at `/g/:token` and have the web webui serve the app. The backend
  exposes `GET /api/v1/guest/invite/{token}` → preview `{group_id, group_name,
  valid}` so the landing page can show the room name before the guest commits.

## Frontend (skchat-app)

- **Operator:** `group_info_screen` gains a "Share link / Invite via link"
  action → calls the invite endpoint → shows the link to copy/share.
- **Guest landing `/g/:token`** (outside the authed shell): if no cached
  identity → prompt name → generate+persist WebCrypto keypair in localStorage →
  `/api/v1/guest/join` → enter room. Returning guest auto-joins from cache.
- **Guest room view:** full conversation kit for the one group (messages +
  reactions/replies + attach/send + download + Join call w/ screenshare) but NO
  bottom nav, NO admin/invite/add-member, NO files browser outside chat, NO peer
  list. Untrusted-guest badge shown. Guest messages signed with the browser key.

## Deferred

- Public ingress / funnel exposure (operator decision later).
- E2EE for guest rooms (the hybrid-E2EE decision applies: public/guest rooms
  would get E2EE — not wired in this pass since we stay on the tailnet).
</content>
