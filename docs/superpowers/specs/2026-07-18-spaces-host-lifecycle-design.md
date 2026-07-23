# Spaces Host Lifecycle (grace auto-end + resume-as-host)

Status: APPROVED behavior (operator, 2026-07-18, "grace + auto-end"). Spec for
implementation planning.

## Problem

Two related gaps in Spaces host lifecycle (verified, see
`scratchpad/spaces-gap-analysis.md`):

1. **Zombie Spaces.** No LiveKit webhook is wired to the server, so when a host
   closes the tab or disconnects (anything other than the explicit `/end` button),
   nothing happens server-side: the Space stays listed "live" forever, and only the
   host can ever end it (`routes.py` `_require_host`). A host who never returns
   leaves a permanent zombie in every directory.
2. **Host cannot resume with tools.** A returning host rejoins as a LISTENER,
   because the app directory always calls `joinListener` (`/spaces/{id}/join`) even
   when the tapping user is the Space's own host. The server already supports
   `/join-host` (grants HOST + roomAdmin only when `requester == host_fqid`); the
   client just never uses it on rejoin. (Fixed as task REJOIN, part of this epic.)

## Decision

- **Grace + auto-end** (operator choice). When the host leaves, start a grace
  window; if the host returns within it, the Space continues and they resume as
  host. If grace expires (or the room empties), the Space auto-ends. Driven by
  LiveKit webhooks so it is real-time.
- **Resume as host** is first-class: a returning host both cancels the pending
  auto-end and regains host tools.

## Design

### Part A: resume-as-host (client, task REJOIN, ship first)

In the app Spaces directory, when `space.hostFqid` equals the local identity (the
same value `_createSpace` uses as `host_fqid`), join via `joinHost(requester=...)`
instead of `joinListener`. Host tools already render off `join.isHost`, so no
room-screen change is needed. Fall back to listener if `/join-host` 403s. Standalone
and independently shippable.

### Part B: LiveKit webhook subscriber (server)

- **Config.** `livekit.yaml` `webhook.urls` posts room/participant events to a new
  skchat endpoint (e.g. `POST /spaces/webhook`). LiveKit signs webhooks with the
  API key/secret; the endpoint MUST verify the signature and reject unsigned or
  mis-signed requests. No unauthenticated state change.
- **Events consumed:** `room_started`, `room_finished`, `participant_joined`,
  `participant_left`. Others ignored. Map the LiveKit room to a Space (the room name
  derives from `space_id`; look it up in the registry).
- **Per-space presence.** From the events, track whether the host identity
  (`host_fqid`) is currently connected and the participant count. This is derived
  state, rebuildable from LiveKit's room list on restart (see reconcile).

### Part C: grace + auto-end state machine (server)

- On host `participant_left` (or host absent while others remain): start a grace
  timer keyed by `space_id`, `SKCHAT_SPACES_HOST_GRACE_SEC` (default 180s).
- Host `participant_joined` within grace: cancel the timer. Space continues.
- Grace expiry: `reg.end(space_id)` (same terminal state as explicit `/end`).
- Room empty (`participant_left` leaves zero participants) or `room_finished`
  (LiveKit tore the room down, e.g. its own `empty_timeout`): end the Space.
- Explicit host `/end` is unchanged and immediate; auto-end is the safety net.
- Timers are in-process (asyncio). On process restart, reconcile: query LiveKit for
  live rooms and end any Space whose room is gone; restart grace for any Space whose
  host is absent. A periodic zombie-sweep (belt-and-suspenders, in case a webhook is
  missed) ends Spaces whose room has been absent from LiveKit for > grace.

### Observability (SK-STD-010)

- Webhook endpoint failures and every auto-end decision are logged with the reason
  (grace-expired / empty / room-finished / sweep) and are alertable.
- The periodic sweep is a wrapped scheduled job (run-ledger + failure to GTD).

## Testing

- Unit: the state machine with an injected clock: host-left then grace-expiry ends;
  host-rejoin within grace cancels; empty-room ends; `room_finished` ends; a
  non-host leaving does not start the timer.
- Webhook auth: unsigned/mis-signed requests are rejected; a valid signed event
  drives the right transition.
- Part A: directory calls `joinHost` when local identity is the host, `joinListener`
  otherwise; 403 falls back to listener.
- Reconcile: on restart, a Space whose LiveKit room is gone is ended.

## Rollout

- Ship Part A (client) first, independently.
- Part B/C: add the endpoint, then set `livekit.yaml` `webhook.urls` + restart the
  SFU. Reversible: remove the webhook config to revert to today's behavior; the
  endpoint is inert without events, and the sweep alone still cleans zombies.

## Out of scope

Co-host and host-handoff (no co-host exists; separate future decision). This epic
keeps the single-host model: the host can leave and come back, and the Space winds
down if they do not.

## Surfaces (for the plan)

- App: `lib/features/spaces/spaces_directory_screen.dart` (+ `spaces_service.dart`) for Part A.
- Server: a new webhook route + a lifecycle module in `src/skchat/spaces/`, the
  registry `end`/lookup-by-room, the reconcile + sweep job, and `livekit.yaml`
  webhook config. No client change for Part B/C.
