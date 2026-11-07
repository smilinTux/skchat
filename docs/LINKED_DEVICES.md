# Linked devices

Moved here from the old agent instructions file. `AGENTS.md` links to it. CLI:
`skchat devices --help`. Code: `src/skchat/device_registry.py`, `device_unlink.py`,
`device_routes.py`, `link_codes.py`, `operator_token.py`.

The operator can list every device enrolled under their identity, rename it, unlink it, and
approve or deny a newly linked one.

## A device lives in four stores

Leaving it in any one is a silent hole:

1. the session revocation set (else its JWTs keep working);
2. its prekey slots on disk (else fanout keeps sealing mail it can decrypt; the worst one,
   because nothing visibly fails);
3. `DeviceStore` (else it can mint a fresh session);
4. its capauth pairing records (else the PDP keeps granting it capabilities).

`unlink_device()` does all four, sessions first. Never re-implement it; `devices reset`
reuses it.

## Approval to link

A newly enrolled device lands **pending**: it cannot mint a session, so it can do nothing,
including publishing a prekey. Nothing auto-approves, so after a `devices reset` the first
device must be approved from the node itself:

```bash
skchat devices pending
skchat devices approve <device_fp>
```

A registry row with no `approved` key reads as approved (grandfathers older devices). A
readable registry with no row for a fingerprint reads as NOT approved; a missing or
unreadable registry reads as approved, so one corrupt JSON file cannot lock every device off
the node.

## Link with a link code, not the operator token

```bash
skchat devices link                 # short-lived single-use code + a scannable QR
skchat devices codes                # how many are outstanding
skchat devices codes --revoke-all
```

`SKCHAT_GUEST_OPERATOR_TOKEN` is a long-lived shared secret: it sits in the per-agent webui
env file, it is presented by other services (`skchat-call-answerer@<agent>`), and it gates
guest invites, prekey signing and the call routes as well as enrollment. A link code is
short-lived and single-use.

A code is accepted in the same header as the operator token, but ONLY on
`POST /api/v1/auth/enroll/open`. It must never widen to the rest of `_require_operator`'s
surface (a test covers exactly that). Only `sha256(code)` is stored. The device still signs
the window with its own key and still lands pending, so a leaked code alone links nothing
usable.

## Rotating the operator token

```bash
skchat operator-token show              # fingerprints only, never the secret
skchat operator-token rotate --yes      # new token, env file, restart consumers
```

Deliberately manual, not on a timer: services present the token, so rotation means
coordinated restarts, and an unattended failed restart breaks the plane with nobody
watching. `show` reports the fingerprint of the running unit's env as well as the file,
which proves a restart picked up the new value. Rotation backs the file up, keeps it
owner-only, and refuses a file with no token line. Rotate on suspected exposure, not on a
schedule.

## The client-side trap (caused a live outage)

Adding a route to `_ROUTE_CAPABILITY_RULES` (`src/skchat/dataplane_auth.py`) makes it
**gated**, and the data-plane gate accepts only `Authorization: CapAuth/Bearer <session>` or
`X-CapAuth-Token`. It does **not** recognise `X-Operator-Token`. A client sending only the
pasted token gets `401 capauth authentication required` before the route's own
`_require_operator` runs, so the feature looks dead against a healthy server. Any new
operator-gated route needs its client to attach `buildOperatorAuthInterceptor`, not just
the token.
