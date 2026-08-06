# Direct-DM Guest Invites - Design Spec

**Date:** 2026-08-06
**Origin:** Chef (David). Brainstormed via superpowers:brainstorming.
**Repos:** `skchat` (server/daemon) + `skworld-app` (Flutter client).
**Next step:** hand to Fable for epic / story / card decomposition (Sonnet-class coding agents).

## Goal
Let the operator send an invite that drops a specific person straight into a **1:1 DM with the operator** - like a Signal contact invite. The guest joins in a browser (no install), gets a display name they can change, and the operator can rename them with a private alias so the operator always knows who it is. The DM supports **text, voice/video, and file transfer**. The relationship is an **ongoing, revocable contact**.

## Why now / does it make sense
Yes. It reframes the earlier "a guest can't search contacts" gap (see coord `57159fb8` Agent Gateway) into a directed, low-friction 1:1 line. And it is mostly **wire-together**, not build-from-scratch: skchat already has the guest primitives.

## Decisions (locked in the brainstorm)
1. **Invite model = BOTH.** Default is single-use, one-invite-per-person (each link is tied to one guest so the alias maps to exactly one person). Also offer a **reusable link** ("my DM link") where each arrival becomes a separate guest DM.
2. **Guest client = web-first, no install.** Guest clicks the link and lands in a browser DM; text + voice/video (LiveKit in-browser) + files all work web-only. The web PQ leg is reduced-assurance (documented, consistent with existing guest web).
3. **Lifetime = ongoing contact, revocable.** History persists; the guest returns via their link (browser-remembered identity) and picks up where they left off; the operator can revoke or set a per-guest expiry.
4. **Sending the invite:** use the platform **native share sheet** (share_plus) so the operator can hand it off via any app, **plus** an explicit **Copy link** action (and QR).

## Defaults (operator can flip in-app; set these as the shipped defaults)
- **Call consent:** a guest's call **rings the operator directly** (the operator invited them). Operator can mute/block. (Alt considered: request-first.)
- **Placement:** guest DMs appear in the operator's normal **Chats** with a **guest badge** + the operator's alias, plus an optional "Guests" filter.
- **Anti-spoofing:** a guest's self-chosen name renders as `guest: <name>`; the operator's **alias always wins** and is visually distinct, so a guest cannot impersonate a real contact.

## Reuse (already built - ground the decomposition here)
- `skchat/src/skchat/guest_accept.py` - Mode-C admission with **`scope=dm`** already supported (macaroon caveats, `_ALLOWED_SCOPES=("dm","group")`, `record_admission`, TOFU pin store).
- `skchat/src/skchat/pq_invites.py` - PQ invite minting (the existing invite modes).
- `skchat/src/skchat/guest_groups.py` / `guest_group_routes.py` / `guest_giftwrap.py` - guest session + routes.
- `skchat/src/skchat/dataplane_paths.py` - `/api/v1/guest`, `/api/v1/mode-c` are exempt (carry their own guest-session auth); reuse, do NOT re-gate.
- `skchat/src/skchat/call_session.py` / `call_routes.py` - LiveKit calling.
- File transfer (existing skchat file endpoints).
- Flutter: `lib/features/join/join_screen.dart`, `lib/features/guest/guest_landing_screen.dart`, `guest_room_screen.dart`, `mode_c_review_screen.dart`.
- `space_share.dart` / share sheet seam (share_plus) already used for Space links - reuse the same share seam.

## New work (the deltas)
### Server (skchat)
- **Mint a dm-scope invite** for the operator: single-use (bound to one admission) or reusable; optional pre-set alias + expiry TTL baked as caveats. Endpoint under the existing operator-gated surface.
- **Direct-DM admission:** the `scope=dm` join lands the guest in a 1:1 conversation with the operator (not a group, not the review-heavy mode-c flow) - minimal/zero friction consistent with the trust model.
- **Alias store** (operator-side, private): map guest identity/fingerprint -> operator alias; served so the client renders the alias. Never exposed to the guest.
- **Guest display name:** persist the guest's chosen name; allow the guest to change it.
- **Revoke / expiry:** per-guest revoke (kills the cap; drops them) and TTL expiry; surfaced to the operator.
- **Scope calls + files to the guest DM:** a guest may call/file ONLY within their DM with the operator (cap scoping); confirm LiveKit room derivation + file endpoints accept the guest session for that one conversation.

### Client (skworld-app)
- **Operator "Invite to DM" flow:** entry from Chats / new-chat; choose single-use vs reusable; optional alias + expiry; produce link + QR; **native share sheet + Copy link**.
- **Guest landing -> name -> direct DM:** on opening a dm-scope link, prompt for a display name (auto-suggested, editable), create/restore the web guest identity, drop directly into the DM.
- **Guest DM rendering:** guest badge + operator alias (fallback to guest name), the `guest:` prefix + alias-wins rule, optional Guests filter.
- **Alias management UI:** rename a guest alias from the DM / contact screen.
- **Revoke / expiry UI:** operator controls per guest.
- **Calls/files in a guest DM:** ensure the call + file affordances are present and work on the web-guest leg.

## Data flow (happy path)
1. Operator mints a dm-scope invite -> `{link, qr, invite_id, mode: single|reusable, alias?, expiry?}`.
2. Operator shares via share sheet / copy link.
3. Guest opens link (browser) -> guest-session bootstrap -> name prompt -> web guest identity -> `scope=dm` admission counter-signed -> 1:1 DM created with the operator.
4. Both sides exchange text; either can start a LiveKit call; either can send files - all scoped to this DM.
5. Guest returns via link -> identity restored from browser -> same DM + history.
6. Operator sets alias / expiry / revoke; revoke invalidates the cap and removes access.

## Error handling
- Reused vs new: a guest-authed request to an operator-only endpoint must fail with an honest permission error, NOT "daemon unreachable" (already fixed for peers in skworld-app #45; keep the pattern for any new guest surfaces).
- Web guest with no PQ backend: reduced-assurance leg, documented; never a hard crash.
- Revoked/expired guest: clear "this invite is no longer active" state, not a generic failure.
- Reusable link abuse: rate-limit new admissions per link; operator can disable the link.

## Testing
- Server: dm-scope invite mint (single + reusable), admission lands a 1:1 (not a group), alias store CRUD (never leaks to guest), revoke kills the cap, expiry enforced, calls/files cap-scoped to the one DM.
- Client: invite flow (share sheet + copy link + QR), guest landing -> name -> direct DM, alias render + `guest:`/alias-wins, revoke/expiry UI, web-guest call + file smoke.
- E2E: operator mints -> guest joins in a fresh browser -> text + call + file round-trip -> guest returns -> operator revokes.

## Open questions for Fable / implementation
- Exact endpoint shapes + where the mint action lives on the operator-gated surface vs the guest-exempt surface.
- Alias store persistence location (operator config vs a small table) and sync across the operator's own devices.
- Reusable-link identity: how repeat visitors are distinguished + de-duped into per-guest DMs.
- Web-guest LiveKit token issuance scoped to the guest DM room.
- Whether to fold this under the Agent Gateway epic (`57159fb8`) or ship standalone first (recommend standalone first; it is the concrete building block the gateway can later route into).

## Success criteria
Operator sends a link (share sheet or copy), a non-technical person opens it in any browser, types a name, and is chatting/calling/sending files 1:1 with the operator within seconds - and the operator sees them under a private alias, with the ability to revoke.

## Group extension (added 2026-08-06, Fable)

Operator request: from inside an existing 1:1 guest DM, invite ANOTHER guest so it becomes a group chat (operator + 2 or more guests); also allow creating a guest group up front with multiple per-person invites or one shared group link. Same trust model, web-first, ongoing and revocable, operator aliases each guest privately. Text + voice/video + files.

### Ground-truth corrections (verified against the real code)
- **Group-scoped guest send/react/file/call ALREADY exist.** Every guest route in `guest_group_routes.py` (`/guest/send`, `/guest/react`, `/guest/file`, `/guest/file/{tid}`, `/guest/call`, `/guest/conversation`) is generic over the session's bound group; nothing about them is dm-only. Reuse as-is.
- **The shared reusable group link ALREADY exists**: `create_group_invite(gid, single_use=False)` (the classic guest-group invite, default mode of `POST /api/v1/groups/{gid}/invite`) admits any number of guests into one group, and a returning browser key dedupes idempotently (`add_untrusted_guest_member` refreshes the same `guest:<slug>#<fp>` member). What it LACKS: rate limiting, the S1 alias sidecar application, S2 contact-registry upsert, and disable-without-nuking semantics.
- **Non-dm guest groups today have NO seat cap and NO epoch fence** (`_dm_epoch_fence` returns None unless `metadata.mode == "dm"`). A new guest joining a classic guest group sees FULL history. The group extension must generalize the fence, not assume it.
- **Group calling and group files are already group-generic**: `daemon_proxy_groupcall.derive_group_room(gid)` + `_mint_guest_call_token` work for any group size; `record_group_transfer`/`transfer_group` scope downloads per group. Only the S6-style operator ring and cap-scoping deltas are new.
- **`daemon_proxy_groups.remove_member(group, identity)` exists**, so per-guest removal without destroying the group is a wire-together.
- **Correction to the earlier promotion sketch** ("mint a new group, migrate the 2 members + history"): rejected after reading the code. Guest session tokens, the transfer allowlist, `dm_contacts.group_id`, the epoch-fence entries in `group.metadata.guests`, and message `thread_id` are ALL keyed by `group_id`. Migrating to a fresh group id breaks every one of them. Promotion is therefore an IN-PLACE mode flip on the same group id.

### Promotion mechanics (decision)
- Inviting a second guest into a `mode="dm"` group promotes it in place: `metadata.mode = "gdm"` (guest group DM), plus `metadata.promoted_from = "dm"`, `metadata.promoted_at = <ts>`, `metadata.seat_cap = <n>`. Same `group_id`; existing guest's session token, history, files, and contact row all keep working untouched.
- Promotion happens at MINT time: `POST /api/v1/groups/{gid}/invite?mode=dm` where `{gid}` is an existing dm/gdm group (today the path id is unused for mode=dm) means "invite another guest into THIS conversation". Minting flips dm to gdm immediately and posts a system notice into the thread.
- Seat cap: `mode="gdm"` enforces `metadata.seat_cap`, default `SKCHAT_GDM_SEAT_CAP` (shipped default 8: operator + up to 7 guests), checked in `guest_join` exactly where `DM_SEAT_CAP` is checked today.
- Epoch fence generalized: fence applies when `metadata.mode` is `dm` OR `gdm`, per guest, keyed on that guest's `added_at` in `metadata.guests`. The two original members predate the flip so they keep full history (history-forward); a newly added guest never sees pre-join messages.
- Existing-guest consent: NOTIFY, not consent-gate. The room is operator-owned (same trust model as the 1:1). A system message ("this conversation is now a group; <name> was invited") lands in the thread at promotion, before the new guest can post, and the guest conversation payload surfaces the mode so the web client shows an audience-changed banner. This is security-sensitive UX: the existing guest must be able to notice the audience changed before sending more.

### Group invite modes (decision: BOTH, mirroring the 1:1)
- **Per-person single-use invites** into the same gdm group: `create_group_invite(gid, single_use=True)` + the S1 jti-keyed alias/contact-ttl sidecar (already jti-keyed, so it applies unchanged). Each invite is aliasable at mint.
- **One shared reusable group link**: `create_group_invite(gid, single_use=False)` + the S2 rate-limit pattern (cap NEW contact creations per jti, generic 401 on trip) + per-fingerprint dedupe (already inherent) + operator disable of the link (revoke the jti) WITHOUT revoking already-admitted members.
- Unlike the reusable 1:1 my-DM-link, arrivals do NOT fan out into separate groups; everyone lands in the one gdm group.

### Aliasing in groups
- The S2 `dm_contacts` registry splits into a per-person registry (fp-keyed: alias, muted, global status) plus a membership table `(fp, group_id, invite_jti, status, added_at)`. Alias is a property of the PERSON (fp), so the same guest carries the same operator alias across their DM and any gdm rooms.
- Operator-side gdm roster and message attribution render alias-wins with the `guest:` prefix fallback, exactly the C3 anti-spoofing rules, now applied per member in a roster. Alias never appears in any guest-facing payload (S1/S4 invariant carries over).

### Calls and files (reuse + deltas)
- Reuse: gdm calls use the existing group room (`derive_group_room`) and the existing `/guest/call` token mint; gdm files use the existing `/guest/file` upload/download with the group-scoped allowlist. No new call or file plumbing.
- Deltas only: the S6 ring (`ring:true` rings the operator, muted contact never rings) extends its dm-only check to dm-or-gdm; guest tokens stay scoped to exactly the one bound group.

### Revoke and expiry
- Per-guest revoke in a group removes ONE member: membership row set revoked + `daemon_proxy_groups.remove_member` + `metadata.guests` cleanup; the S3 chokepoint then 403s that guest. The group and every other member survive.
- Global (person-level) revoke keeps the S4 semantics: kills the person everywhere.
- Group-level expiry: optional `metadata.expires_at` on a gdm group, enforced at the same chokepoint; when expired, all guest access 403s with a clear reason while operator history is retained.
