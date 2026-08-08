# Device Management ("Linked Devices") Design

**Date:** 2026-08-02
**Author:** Lumina (with Chef)
**Status:** Approved design, ready for planning
**Repo:** skchat (+ skworld-app for the UI)

## Goal

Give the operator (Chef) a "Linked Devices" surface, the same mental model as
Signal/WhatsApp linked devices, to see every device registered under their
identity and unlink any of them. Unlinking must be security-correct: the
unlinked device immediately loses the ability to authenticate AND to decrypt
new messages.

## Background: the current state (from the infra audit)

A single physical device is represented by **three unlinked identifiers** spread
across **four stores**, with no shared correlation key:

| Identifier | Where | Derived from |
|---|---|---|
| `device_fp` (16 hex) | enrollment (`DeviceStore`), operator-session JWT claim, capauth subject `operator:<fp>` | `sha256(device_pubkey_b64)[:16]` of the ECDSA **auth** key |
| `key_id` (16 hex) | prekey slot filename `peers/<short>/<key_id>.json` | first 16 hex of the hybrid **KEM** public key |
| `device_id` (`dev-<base36>`) | field inside a prekey slot | app wall-clock at service construction (throwaway) |

Four stores:
1. `~/.skchat/state/operator_devices.json` (`DeviceStore`): a flat `dict[device_fp -> pubkey_b64]`. **No metadata, no list method, no delete method.**
2. `~/.skcapstone/peers/operator:<fp>.json`: capauth pairing `DeviceRecord` (has `approved_at`, `revoked`, supports `list_devices`/`revoke`, but keyed per-subject).
3. `~/.skchat/guest_revocations.db`: the `revoked_jtis` table (jti-only revocation, shared by guest invites and operator sessions).
4. `~/.skchat/pqc/peers/<short>/<key_id>.json`: prekey slots (the fanout targets).

**Two gaps that shape the design:**
- **Sessions are stateless JWTs with no server-side registry.** There is no way to enumerate or revoke *all* of a device's sessions today; you can only revoke a jti you happen to hold, or wait out the ≤24h expiry.
- **Nothing records which prekey slot(s) belong to which device.** The publish call (`api_publish_prekey`) is authenticated with the session's `device_fp` via `require_dataplane_auth`, but it discards it.

## Approved design decisions

1. **Session kill via a revoked-`device_fp` set**, not per-jti tracking. `verify_operator_session` rejects any session whose `device_fp` is in the set. Unlinking a device adds its fp once and every one of its sessions dies immediately. Re-enrolling clears it. (Chosen over an issued-jti registry: simpler, and it revokes all of a device's sessions atomically.)
2. **Device registry built at publish time** from the authenticated session's `device_fp`. The publish handler is the join point: it reads `device_fp` from the verified session and records `device_fp -> {key_ids, metadata}`.

### Revisions from the 2026-08-08 planning pass (supersede the above where they conflict)

These four points were settled by checking the design against the live code and
the live state on .158. Each one replaces an assumption that did not survive
contact.

**R1. Clean cut for pre-registry devices (was unspecified).**
The live box carries **13 enrolled devices** in `DeviceStore` and **6 `chef`
prekey slots**, with *zero* information to correlate them: a slot holds
`key_id`, a throwaway `device_id` (`dev-mse0cszf`, wall-clock at construction)
and `last_published`, but **never** `device_fp`. The registry is a join table
built going forward, so for every currently-enrolled device `key_ids` would be
empty, and step 2 of `unlink()` would find no slots to remove. Sessions would
die and the `DeviceStore` entry would go, while the device's KEM slot survived
and Lumina's fanout kept sealing new messages to it. That is exactly the
"partial unlink is a silent security hole" this design exists to prevent, and it
would be the day-one default for every device.

Resolution: a deliberate one-time reset, then re-link. **Not** an automatic wipe
on upgrade, which would silently lock out every device the moment the new code
deploys. A CLI command, run when the operator is ready:

```
skchat devices reset --yes   # clears DeviceStore + chef prekey slots + capauth operator grants
```

After the reset the registry is authoritative and complete. There is no "legacy
/ unidentified device" state anywhere in the model, and no legacy branch in the
code, ever.

**R2. The enrollment label is client-sent and signed (replaces "no new client
fields" in decision 2).**
Deriving the label from `User-Agent` alone does not work: the app sets no
`User-Agent` on its API client, so every native build (phone, desktop) enrolls
as an identical `Dart/3.x (dart:io)` row, distinguishable only by fingerprint.
That defeats the purpose of the screen.

The enroll body gains an optional `label`, and the signed payload becomes
`{nonce, device_pubkey, label}`:
- `label` present -> the signature MUST cover it, so it cannot be rewritten in
  transit.
- `label` absent -> the server verifies the **existing** two-field payload and
  derives the label from `User-Agent`. The current web build keeps working
  unchanged, so shipping the server is not a flag day for the client.

The label is self-asserted, so it renders with untrusted styling until the
operator renames it. Phase 2's rename is the trusted override: operator-authored
name wins, the same rule as `guestDisplayTitle(alias, name)`.

**R3. `device_fp` must be plumbed to the publish handler (gap in decision 2).**
Decision 2 says the publish handler "already has the session in hand". It does
not: `require_dataplane_auth` is `async def ... -> None` and discards the
verified session. One seam closes it: `enforce_dataplane_auth` stashes the
verified session on `request.state.operator_session`, and `api_publish_prekey`
reads `device_fp` from there. No route signature or client contract changes.

**R4. Unlink reuses the existing prekey-revoke primitive (no new mechanism).**
`pq_prekeys.remove_peer_bundle(peer, key_id)` already exists and already backs
the operator route `DELETE /v1/prekey/{peer}/{key_id}`
(`daemon_proxy.api_revoke_prekey`). `unlink()` calls that same function
in-process. This is the shared mechanism the S2/S3 coordination note asks for,
so the multi-device revoke path and the unlink path cannot diverge.

Two spec assumptions were checked and are already true, so they need no work:
`guest._require_operator` **already** accepts an enrolled-operator session
Bearer (its "Path 2"), and that same presented session is where `is_current`
reads the caller's own `device_fp`.

## Architecture

### The device registry (the foundation)

A new store `~/.skchat/state/operator_device_registry.json`, keyed by `device_fp`:

```json
{
  "a2d3...": {
    "device_fp": "a2d3...",
    "label": "Chrome on Linux",
    "label_source": "derived",
    "platform": "web/linux/chrome",
    "user_agent": "Mozilla/5.0 ...",
    "enrolled_at": 1754160000.0,
    "last_seen": 1754170000.0,
    "key_ids": ["f8342853f762fd88"],
    "revoked": false
  }
}
```

- Written on **enroll**: `label` from the signed client field when present, else derived from the request `User-Agent` (R2); `platform` from the `User-Agent`; `enrolled_at` set. A `label_source` field records `"client"` or `"derived"` so the UI knows to style it untrusted, and Phase 2's rename sets it to `"operator"` (trusted, wins over both).
- `key_ids` + `last_seen` updated on every **prekey publish** (the handler reads `device_fp` off `request.state.operator_session` per R3 and appends the bundle's `key_id`).
- `last_seen` also bumped on any authenticated request (cheap, best-effort).
- The registry is the join table that makes "unlink this device" able to find that device's prekey slots.

### Session revocation set

A `revoked_device_fps` set. Simplest home: a new table `revoked_device_fps(device_fp PRIMARY KEY, revoked_at REAL)` in the existing `guest_revocations.db`, with an in-memory cache mirroring the existing `_revoked_cache` pattern. `verify_operator_session` gains one check: reject if `device_fp` is revoked (alongside the existing `_is_revoked(jti)` check).

### Unlink semantics (atomic-ish, best-effort per store)

`unlink(device_fp)` performs, in order:
1. Add `device_fp` to `revoked_device_fps` (kills all its sessions instantly).
2. Remove every prekey slot in that device's `key_ids` via `pq_prekeys.remove_peer_bundle` (the SAME primitive behind `DELETE /v1/prekey/{peer}/{key_id}`, per R4), so Lumina's fanout stops sealing to it on the next message.
3. Delete the device from `DeviceStore` (so no NEW session can be minted).
4. Best-effort `capauth.pairing.revoke(...)` for `operator:<fp>` (revokes the `skchat.prekey` grant).
5. Mark the registry record `revoked: true` (kept for audit, filtered out of the default list).

Each step is independently safe to retry; a partial failure leaves the device *more* locked out, never less.

## Phases

### Phase 1 (foundation + MVP: list + unlink), build first

Server (skchat):
- Device registry store module (`device_registry.py`): load/save, `record_enroll`, `record_publish(device_fp, key_id, user_agent)`, `touch(device_fp)`, `list()`, `mark_revoked`.
- Revoked-fp set in `guest.py`/a small module: `revoke_device(fp)`, `is_device_revoked(fp)`; wire the check into `verify_operator_session`.
- **(R3)** Stash the verified session on `request.state.operator_session` in `enforce_dataplane_auth`.
- **(R2)** Accept an optional signed `label` in `POST /api/v1/auth/enroll`: when present the signature must cover `{nonce, device_pubkey, label}`; when absent verify the existing two-field payload and derive from `User-Agent`.
- Wire `record_enroll` into the enroll route and `record_publish` into `api_publish_prekey` (read `device_fp` off `request.state.operator_session`).
- **(R1)** `skchat devices reset --yes` CLI: clear `DeviceStore`, the operator's prekey slots, and the capauth operator grants. Refuses to run without `--yes`; prints exactly what it will delete first.
- Endpoints (operator-gated via `_require_operator`, which now accepts the operator session Bearer):
  - `GET /api/v1/operator/devices` -> `[{device_fp, label, platform, enrolled_at, last_seen, key_ids, is_current}]` (current resolved from the caller's own session `device_fp`).
  - `DELETE /api/v1/operator/devices/{device_fp}` -> runs `unlink(...)`; 400 if it's the caller's own current device (guard against self-lockout; a device unlinks *others*, not itself).
  - `POST /api/v1/operator/devices/unlink-others` -> unlink every device except the caller's current `device_fp`.

App (skworld-app):
- `DeviceListService` (Dio + operator auth interceptor) calling the three endpoints.
- "Linked Devices" screen under the Me tab: list rows (label, last-seen relative time, "This device" chip on the current row), per-row **Unlink** with a confirm dialog, and an "Unlink all other devices" button. Current-device row has no Unlink.
- **(R2)** Send a real `label` at enrollment, derived from actual device info (model / OS / host), signed with the nonce and pubkey. A row whose `label_source` is not `"operator"` renders with untrusted styling, consistent with the guest-name anti-spoof rule.

Independently useful and testable on its own.

### Phase 2 (rename)

- `PATCH /api/v1/operator/devices/{device_fp}` `{label}` -> label override in the registry.
- Inline rename affordance in the list row.

### Phase 3 (approval-to-link)

- New enrollments land in a **pending** state; their prekey slot is quarantined (stored but excluded from Lumina's fanout, and `is_device_revoked`-style gated) until approved.
- `GET /api/v1/operator/devices/pending`, `POST .../{fp}/approve`, `POST .../{fp}/deny`.
- Pending-device banner + approve/deny UI on an existing (approved) device.
- Changes the enrollment trust model, so it ships last.

## Error handling

- All new endpoints operator-gated; a non-operator gets 401/403 exactly as the existing operator routes.
- Unlink of a non-existent `device_fp` -> 404.
- Unlink of the caller's own current device -> 400 with a clear message (use "unlink others" or unlink from another device).
- Registry/store writes use the existing atomic tmp+`os.replace` pattern; a read of a corrupt/missing registry degrades to an empty list, never a 500.
- Revocation is fail-safe: if a step fails, the device is left more locked out, and the error is logged, not surfaced as success.

## Testing

- Registry: enroll records a device; publish links a `key_id`; `list()` shape; `mark_revoked` filters.
- Revoked-fp: `verify_operator_session` rejects a revoked fp; a fresh enroll clears it.
- Unlink: removes prekey slots (fanout no longer seals to it), revokes sessions, deletes DeviceStore entry; idempotent; self-unlink guarded.
- Endpoints: operator-gate enforced; current-device marking correct; unlink-others spares the caller.
- App: list renders, unlink calls the endpoint + refreshes, current row non-removable (widget tests mirroring the existing service-test style).
- **(R2) Label signature binding:** an enroll carrying a `label` NOT covered by the signature is rejected 401. A tampered label (valid sig over a different label) is rejected. A label-less enroll still succeeds on the old two-field payload.
- **(R3) Plumbing:** `api_publish_prekey` records the `key_id` against the device_fp of the session that actually authenticated the call, proven by publishing under two different device sessions and asserting the slots land on different registry rows.
- **(R1) Reset:** the CLI clears all three stores; a subsequent enroll produces a registry row with a populated `key_ids` after its first publish. **The end-to-end security assertion:** enroll two devices, publish from both, unlink one, then assert a fresh fanout seals to the survivor's slot ONLY and the unlinked device's slot file is gone. That is the test the whole epic exists for; it must fail if any single unlink step is skipped.

## Out of scope (this design)

- Cross-node device sync semantics (the registry is per-node like the other skchat state; a device is managed on the node it talks to).
- Push notifications on new-device link (could layer on Phase 3 later).
