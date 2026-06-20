# Federation FQID realm — canonical form (coord F0-fqid · 84fb38da)

## The problem

Cross-realm conf-token mint needs the signing agent's **emitted FQID** to match
the **pinned-key filename** in `~/.skchat/federation-peers/<fqid>.asc`. If they
diverge, `verify_signed` → `keystore.federation_pubkey(fqid)` returns `None` and
the redeem fails with `no pubkey for fqid`.

Two namespaces were in play:

| namespace | example | where |
|-----------|---------|-------|
| capauth **wire** URI | `capauth:lumina@skworld.io` | `AgentIdentity.capauth_uri`, peer registry, transport |
| sovereign **FQID** | `lumina@chef.skworld` | federation trust, pin filenames, `Assertion.fqid` |

## Decision: canonical = `<agent>@<operator>.<realm>` = `@chef.skworld`

Keep the existing pins/trust form. It requires **zero code churn** and is already
what the signing path emits.

### What the signing path actually emits

`resolve_agent_identity()` (`capauth/src/capauth/agent_identity.py:226`) returns
`AgentIdentity.fqid = "<agent>@<operator>.<realm>"`, built from
`~/.skcapstone/cluster.json` (`operator: chef`, `realm: skworld`) →
`lumina@chef.skworld`, `jarvis@chef.skworld`.

The conf-call mint entry point `call_routes._self_fqid()`
(`src/skchat/call_routes.py:41-44`) returns exactly `resolve_agent_identity().fqid`,
and `FederationDiscovery.build_signed_assertion(fqid=...)`
(`src/skchat/spaces/federation/discovery.py:132`) puts that string verbatim into
`Assertion.fqid`. So the emitted FQID is **already** `@chef.skworld` — matching
both `federation-trust.json` (`full_access`) and the pin filename convention
(`keystore._safe_name` keeps `.`). **No code change is needed on the sign path.**

The `capauth_uri` (`@skworld.io`) is the *wire* identity for the peer registry /
transport only; it is never used as a federation FQID. So the apparent
"`@chef.skworld` vs `@skworld.io` mismatch" is a namespace confusion, not a code
bug — the two are intentionally different and the federation layer uses the FQID.

## The real gap: missing pins (config, not code)

`~/.skchat/federation-peers/` only had `jarvis@chef.skworld.asc`. There was **no
`lumina@chef.skworld.asc`** (nor opus / chef). So a remote peer asked to verify a
*lumina* assertion against `lumina@chef.skworld` would hit `no pubkey for fqid`.

Fix is purely config: every agent in `full_access` that can be a *signer* must
have its pubkey pinned on the verifier box, keyed on its canonical FQID. The
agent's own pubkey lives at
`~/.skcapstone/agents/<agent>/capauth/identity/public.asc`.

### `scripts/align_federation_fqid.sh`

Idempotent. For every agent under `~/.skcapstone/agents/*/capauth/identity/public.asc`
whose canonical FQID (`<agent>@<operator>.<realm>` from `cluster.json`) is in
`federation-trust.json::full_access`, copy its `public.asc` to
`~/.skchat/federation-peers/<fqid>.asc` (only if absent or changed). Also ensures
`federation-trust.json` exists with the canonical defaults. Run on **.158** and
**.41** so both boxes can verify each other's signers.

## Verified round-trip (this box, 2026-06-20)

`tests/test_fed_fqid_realm.py::test_real_capauth_roundtrip_lumina` signs a live
assertion with lumina's capauth private key (canonical fqid `lumina@chef.skworld`),
pins lumina's pubkey under that fqid, and verifies via the real
`keystore.federation_pubkey` + `_default_verify`. PASS.

The committed `jarvis@chef.skworld.asc` pin is byte-identical to jarvis's local
`public.asc` (`diff` → IDENTICAL), so a jarvis-signed assertion verifies the same
way.

## Residual risk

- **Jarvis private key** is present on *this* box (.158) but in production jarvis
  runs on chiap04/.41 — the round-trip for a *real* jarvis signature can only be
  exercised where jarvis's private key lives. The pin (its **pubkey**) is what
  this box needs to *verify* jarvis, and that is correct.
- If `cluster.json` ever changes `operator`/`realm`, every pin filename and the
  trust list must be re-aligned — rerun the script.
