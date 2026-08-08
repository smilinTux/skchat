# Prekey signature identity: scoped operator attestation

Design doc. Card `1afc178c`. Unblocks the `SKCHAT_REQUIRE_SIGNED_PREKEYS` flip,
the last open item of the multi-device DM fanout epic (card `acf8e713`).

Date: 2026-08-08

## Background: the card's premise is out of date

The card says the flip is blocked because app-published bundles will not verify:
the app publishes with no `owner`, the server defaults `owner=chef@skworld.io`,
and `chef.json` carries no `public_key`.

That is no longer the failure. Commit `76799d8` gave `_resolve_signer_pubkey` a
fallback to the daemon agent key, and bundles minted through the operator
signing oracle are signed with exactly that key. Verified read-only on .158 on
2026-08-08 by simulating the flag against the live store: **all 6 live `chef`
slots ACCEPT.**

Two different problems are the real blockers.

### Problem 1: the green state is accidental

`_resolve_signer_pubkey` tries two sources in order:

1. the peer store (`~/.skcapstone/peers/<owner>.json` -> `public_key`)
2. the daemon agent key (`load_agent_crypto`)

chef verifies only because source 1 misses: `~/.skcapstone/peers/chef.json` does
not exist. Eight peers in that store already carry a `public_key` (`opus` has a
3931-char one). The day anyone creates `chef.json` with chef's own PGP key,
source 1 starts winning and every app publish fails closed. Nothing in the code
or the docs records that the working state depends on a file being absent.

### Problem 2: `owner` is effectively unauthenticated under the flag

The fallback is unscoped, so it fires for any owner missing from the peer store.
Measured on .158:

| owner | resolves to |
|---|---|
| `chef` | daemon-fallback |
| `mallory` | daemon-fallback |
| `totally-made-up` | daemon-fallback |
| `opus` | peer-store (3931 chars) |
| `lumina` | daemon-fallback |

So under the flag, a bundle published under *any* owner name absent from the
peer store verifies against the daemon key. The signature attests "the operator's
daemon signed this", not "this owner owns this key". The oracle is operator-gated,
so this is not remotely exploitable, but it means the flag does not buy the
identity binding it appears to buy, and behaviour silently differs per owner.

### Blast radius of the flag (measured, not assumed)

The flag gates exactly one path: `store_app_prekey_bundle`, behind
`POST /api/v1/prekey`. Its only caller fleet-wide is the Flutter app
(`pq_prekey_service.dart:325`).

`publish_self_prekey` calls `store_peer_bundle` directly and therefore bypasses
the flag entirely. This is load-bearing: lumina's and opus's live slots are
UNSIGNED and stay working after the flip. Federated prekeys arrive over
`prekey_exchange.py`, which verifies opportunistically on its own path.

## Goals

- Make the operator path verify *by design* rather than by accident.
- Give the flag the identity meaning it claims.
- Make the flip decidable from evidence instead of from a guess about which app
  builds are deployed.

## Non-goals

Explicitly out of scope, and none of them are needed under this model:

- populating `chef.json`
- changing what the app signs with, or where the app's signing key lives
- any change to the bundle wire format
- signing the currently-unsigned `lumina` / `opus` / `bob` / `test` self-published
  slots (they do not traverse the gated path)

## Design

### 1. Scoped operator attestation

Replace the silent fallthrough in `_resolve_signer_pubkey` with an explicit branch:

```
owner == short(OPERATOR_ID)  ->  daemon attestation key, and only that
otherwise                    ->  peer-store key, or None (reject)
```

The signature then means one stateable thing: **an authenticated operator session
attested this device bundle.** For the operator's own devices that is the correct
claim, and it is why the signing oracle exists at all: the app cannot hold the
operator identity key.

Consequences:

- chef no longer depends on `chef.json` being absent. Creating that file with any
  key cannot break publishing.
- `mallory` / `totally-made-up` no longer resolve to a valid signer.
- Non-operator owners must self-sign against their peer-store key. That is the
  correct requirement and matches what the federation path already does.

### 2. Mode tri-state, back-compatible

`require_signed_prekeys()` is boolean today. Add `prekey_verify_mode()` next to
it, mirroring `dataplane_auth.authz_pdp_mode()` (`dataplane_auth.py:277`), which
is this repo's existing idiom for staging exactly this kind of rollout.

| `SKCHAT_REQUIRE_SIGNED_PREKEYS` | mode | behaviour |
|---|---|---|
| unset, or unrecognized | `off` | store, no verify (unchanged default) |
| `shadow` | `shadow` | verify, log the outcome, **store anyway** |
| `1` `true` `yes` `on` (existing truthy set) | `enforce` | verify, reject on failure |

The existing truthy set is `{"1", "true", "yes", "on"}` and keeps meaning
enforce, so no current reader changes behaviour. Read at call time, so a rollout
can be staged without a reimport. `require_signed_prekeys()` stays as a thin
`mode == "enforce"` wrapper for its existing call sites.

**The intake call site must become mode-aware, not boolean.** Today it reads:

```python
signer = _resolve_signer_pubkey(owner) if PQ.require_signed_prekeys() else None
```

Under `shadow` that predicate is false, so no signer would resolve and every
bundle would log a false `REJECT`, making the soak worthless. The signer must be
resolved whenever the mode is `shadow` **or** `enforce`, and only skipped when
`off`. `store_app_prekey_bundle` correspondingly switches on the mode rather
than on a boolean: verify-and-store for `shadow`, verify-and-gate for `enforce`,
store-as-is for `off`.

### 3. Shadow observability

The reason a blind flip is risky: a rejected publish surfaces only as a warning,
so "who breaks if I flip this" is currently unanswerable. In `shadow` and
`enforce`, every intake logs one line under a stable grep prefix:

```
prekey-verify mode=shadow owner=chef kid=f8342853 signer=daemon-attest result=ACCEPT
prekey-verify mode=shadow owner=opus kid=9ba5c0cb signer=peer-store  result=REJECT reason=unsigned
```

Fields: `mode`, `owner`, truncated `key_id`, which signer source resolved
(`daemon-attest` / `peer-store` / `none`), `result`, and on failure a `reason`
(`unsigned` / `bad-signature` / `no-signer-key`). No key material is logged.

This makes the flip criterion concrete and checkable from `journalctl` alone:
a soak window with zero `result=REJECT`.

### 4. Failure modes

Under `enforce`, all fail closed, and all three are reachable today:

| condition | outcome |
|---|---|
| no signer key resolves | reject, `reason=no-signer-key` |
| signature present, verification fails | reject, `reason=bad-signature` |
| no signature on the bundle | reject, `reason=unsigned` |

`shadow` never rejects, so a defect in the new resolver cannot take the app
offline during the soak. Rejection remains a 400 from `POST /api/v1/prekey`; the
client already tolerates a failed publish and retries on next startup.

### 5. Testing

Resolver:
- operator owner resolves to the daemon attestation key
- non-operator owner with a peer-store key resolves to that key
- non-operator owner without one resolves to `None`
- **regression that motivates the change:** operator owner still resolves to the
  daemon key when `chef.json` exists carrying a *different* key
- an arbitrary unknown owner (`mallory`) resolves to `None`

Mode parse:
- unset -> `off`; `shadow` -> `shadow`; each existing truthy value -> `enforce`;
  unrecognized -> `off`
- `require_signed_prekeys()` stays true exactly for `enforce`

Store path:
- `shadow` stores an unsigned bundle and logs `REJECT`
- `enforce` rejects the same bundle and stores nothing
- `off` stores without verifying (unchanged)
- **the shadow-soak-is-meaningful test:** in `shadow`, a *validly signed* operator
  bundle logs `ACCEPT`, proving the signer is actually resolved in shadow rather
  than left `None` (the defect the call-site note above exists to prevent)

Bypass invariant:
- `publish_self_prekey` stores successfully in `enforce` mode, since lumina's
  unsigned self-published slot must keep working

Live evidence, pinned as a fixture test:
- the 6 real chef slots verify under the daemon attestation key

### 6. Rollout

1. Land scoped attestation + mode tri-state with the flag still unset. No
   behaviour change; deployable on its own.
2. Deploy to .158, restart `skchat-webui@lumina`.
3. Set `SKCHAT_REQUIRE_SIGNED_PREKEYS=shadow`. Soak.
4. Read the log. Every distinct publishing device should appear with
   `result=ACCEPT`.
5. Fix anything that appears as `REJECT`.
6. Flip to `1`. Confirm a signed publish is accepted and an unsigned one is
   rejected.

The card's "every fleet app build must ship #37 first" concern resolves itself
here: shadow answers empirically whether any deployed build still publishes
unsigned, instead of requiring us to enumerate builds we cannot see from the
daemon.

## Rollback

Unset the env var. The code path returns to store-without-verify. No data
migration, no stored state changes shape, so rollback is a restart.
