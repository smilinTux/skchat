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
