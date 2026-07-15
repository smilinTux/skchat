# skchat Sovereign Invite / Join / Guest Architecture

> Research-swarm output (5 researchers: SimpleX, Nostr, Signal/Matrix, P2P/OOB, best-practices; synthesized + 4-lens adversarial review). Design doc, additive + flag-gated. 2026-07-15.
Grounded in the real code. Here is the architecture doc.

---

# skchat Invite / Join / Guest Architecture — 3 Modes (Guest, Federated, Non-Federated)

Status: design, additive + flag-gated. Extends existing modules, no rewrite.
Anchors in code: `src/skchat/guest_groups.py`, `guest_group_routes.py`, `join_routes.py`, `app_link.py`, `call_session.py`, `pq_prekeys.py`, `prekey_sig.py`, `prekey_exchange.py`, `dm_ratchet.py`, capauth (`agent_profile.resolve_agent_identity`), skcomms transports (file / tailnet S2S `/inbox` / Nostr discovery / coturn Funnel :8443).

---

## 0. What skchat already has (the baseline we build ON)

| Capability | Where | Note |
|---|---|---|
| Invite token = HS256 JWT (`jti,iss,tier,group_id,iat,exp,once`) | `guest_groups.create_group_invite` | Server-secret signed; **does not** carry inviter identity or crypto material |
| `join_url = /app/#/g/<token>` | same | **Token already lives in the URL fragment** (hash route) — fragment-secrecy is half-done |
| Single-use burn + TTL + revoke | `verify_group_invite(burn_single_use=)`, `guest._is_used/_is_revoked/_mark_used`, `operator_revoke_invite` | Atomic burn on first accept; JWT `require` list; generic 401 (no oracle) |
| Guest keypair (client-gen) + join | `POST /api/v1/guest/join {invite_token,display_name,guest_pubkey}` | Guest is untrusted MEMBER, scoped to one `group_id`, session JWT, LiveKit token, history bootstrap |
| Untrusted scoping | `add_untrusted_guest_member` (trust=untrusted, never ADMIN), `_deny_cross_group` (403 on any other group) | Structural one-room isolation |
| Advisory guest message signing | `canonical_sign_payload(group_id, body, ts)` | Guest browser signs `{body,group_id,ts}` |
| PQ prekey bundles | `pq_prekeys.store_app_prekey_bundle`, `POST/GET /api/v1/prekey/<short>`, suite `x25519-mlkem768` (`hybrid_public_hex`) | Signed-bundle gate `SKCHAT_REQUIRE_SIGNED_PREKEYS` (fail-closed) via `prekey_sig.verify_prekey_bundle` (PGP armor) |
| RFC-0001 ratchet | `dm_ratchet.py` | hybrid x25519+ML-KEM-768, concatenate-then-KDF, epoch ratchet, downgrade-locked, group sealing live |
| Federation fetch | `prekey_exchange.fetch_peer_prekey` | GET remote `/api/v1/prekey/<short>`; **stays classical if no federation route** |
| Identity | `resolve_agent_identity → capauth:<agent>@skworld.io` + FQID `<agent>@<operator>.<realm>` | capauth PGP; Bunker remote-signer |

**The three gaps we close (all additive):**
1. The invite JWT is signed by the *server secret*, not the *operator identity*, and commits to **no** crypto. So a compromised daemon can forge invites and the guest's first message is not guaranteed E2EE. → add an **operator-signed inner assertion with a PQ bundle commitment**.
2. Guest join is **group-only**; there is no 1:1. → model 1:1 as a **degenerate 2-seat guest group** (`mode=dm`), reuse everything.
3. No **accept/sign** primitive for an unfederated peer who has an identity. → add a **mutual peer-signed accept assertion** carried over Nostr/Funnel.

---

## 1. Common primitives (shared by all three modes)

### 1.1 Token & URL format — secret in the fragment, commitment in the signature

Keep the existing HS256 JWT as the **server-side routing/burn envelope** (unchanged — burn, TTL, revoke all keep working). Wrap it with two additions:

```
URL:  https://<origin>/app/#/g/<token>&k=<link_key_b64u>

token (JWT, unchanged envelope + 2 new claims):
  { jti, iss, tier, group_id, iat, exp, once?,
    ik_fp:  "<operator capauth/ML-DSA identity-key fingerprint>",   // NEW
    bc:     "<base64url SHA-256 of the operator PQ prekey bundle>",  // NEW: bundle commitment
    idm:    "capauth:<agent>@skworld.io",                            // NEW: dual-URI inviter
    mode:   "dm" | "group" }                                         // NEW

k (fragment-only, NOT in the JWT, NEVER sent to the server):
  32B random link_key. Used two ways:
    - unlocks server-held "link data" (SimpleX v6.4 pattern, see 1.4), and
    - salts the guest→operator handshake so a URL-log leak alone can't join.
```

Rules (from best-practice + SimpleX briefs):
- **Everything secret is after `#`.** `token` is already there (`/app/#/g/`); `k` joins it. Browsers never put the fragment in the HTTP request → Funnel/CDN/daemon access logs never see joinable secrets.
- **`bc` (bundle commitment) is the anti-downgrade lock.** The joiner fetches the operator prekey bundle and MUST verify `SHA256(bundle) == bc` before running PQXDH. An absent or all-classical bundle becomes a **verify failure, not a silent classical fallback** — closes the known PQXDH KEM-stripping downgrade even against a malicious relay.
- **`ik_fp` + operator signature** make the invite self-authenticating against the *operator's identity*, not just the server secret. Add an operator detached signature over the canonical token claims (capauth PGP now; ML-DSA composite when `sk_pgp` lands) as `sig` alongside the JWT. Verifier checks both: JWT (routing/burn) **and** operator sig (identity/anti-forge).

New helper (extends `guest_groups.py`):
```python
def create_pq_invite(group_id, *, mode="group", single_use=True, ttl=None, crypto) -> dict:
    # 1. ensure operator self prekey published (pq_prekeys.publish_self_prekey)
    # 2. bc = b64u(sha256(canonical(bundle)))
    # 3. token = create_group_invite(...) + claims {ik_fp, bc, idm, mode}
    # 4. sig = prekey_sig-style operator detached sig over canonical(token-claims)
    # 5. link_key = secrets.token_bytes(32); store link-data keyed by jti (1.4)
    # returns {token, sig, join_url: f"/app/#/g/{token}&k={b64u(link_key)}", jti, bc, mode}
```

### 1.2 Identity from the link (PQ handshake)

Joiner path (guest OR peer), on opening the link:
1. Split fragment → `token`, `k`. Decode JWT claims (no verify yet) to read `idm`, `bc`, `mode`, `ik_fp`.
2. Preview/verify: `GET /api/v1/guest/invite/{token}` (existing `guest_invite_preview`) — extend to also return `{idm, ik_fp, bc, mode, operator_sig}` and the **server-held link-data ciphertext** (1.4). Verify operator `sig` under `ik_fp`.
3. Fetch operator prekey bundle (`GET /api/v1/prekey/<short-of-idm>`), assert `SHA256(bundle)==bc`. **Fail → abort, red banner.** This is the downgrade lock.
4. Generate an ephemeral hybrid keypair (x25519 + ML-KEM-768 encapsulation to the bundle's `hybrid_public_hex`), run the **same PQXDH the DM ratchet uses** (`dm_ratchet`), mixing `k` into the KDF `info` so the fragment secret is required. First message is E2EE + HNDL-safe from message one.
5. Show a **SAS / safety-number** (short hash of both sides' identity keys) in `guest_landing_screen` for out-of-band comparison. This is the *only* defense against a swapped-link MITM on the sharing channel (no directory to check against).

### 1.3 Guest scoping (unchanged model, extended to DM)

Reuse `add_untrusted_guest_member` + `_deny_cross_group` verbatim. A DM is a group with exactly 2 seats and `metadata.mode="dm"`. Guest capability floor stays:
- role MEMBER, never ADMIN; `trust="untrusted"`, `guest=true`.
- cannot add members, cannot rotate group keys, cannot read history before its epoch (add an epoch fence: `_guest_messages` filters `ts >= member.added_at` for DM/guest — SimpleX "no pre-epoch history").
- messages tagged `sender=unverified` in UI until SAS confirmed.
- scoped session JWT (`mint_guest_session`) → one `group_id`; every route re-checks via `_deny_cross_group`.

### 1.4 Server-held link data (SimpleX v6.4 borrow) — optional hardening

Instead of putting `bc`/`idm` in the JWT plaintext, store an **encrypted link-data blob keyed by `jti`**, decryptable only with `k`. `guest_invite_preview` returns the ciphertext; the joiner decrypts locally with the fragment `k`. **First read locks the blob to the accepter's ephemeral key** (reuse the single-use burn: `_mark_used(jti)` on first `GET` when `once`). Effect: an observer of the URL who lacks `k` learns nothing (not even which operator/bundle), and a leaked link can't be silently used twice. Ship this in Phase 3 (nice-to-have; the JWT-claims version works day one).

### 1.5 Delivery (skcomms)

- **Guest, off-tailnet**: coturn **Funnel :8443** (already the guest reach path) for the HTTPS `/api/v1/guest/*` calls + LiveKit media. Nothing new.
- **Federated peer**: tailnet **S2S `/inbox`** + `fetch_peer_prekey` over the federation route.
- **Non-federated peer**: **Nostr discovery relay** as a dumb rendezvous for the accept/sign round-trip, wrapped so the relay sees only ciphertext (mode C, §4).

---

## 2. MODE A — Guest / anonymous join from a shareable link (1:1 + group)

**Goal:** operator pastes a link into SMS/email; an anonymous stranger opens it and starts a 1:1 (or group) session, PQ-safe, no account.

**Invite (operator side):**
- Group: `create_pq_invite(group_id, mode="group", single_use=…)`.
- 1:1: operator calls a new `create_dm_invite()` that first mints a **2-seat DM group** (`daemon_proxy_groups`, `metadata.mode="dm"`, seat 1 = operator) then `create_pq_invite(dm_group_id, mode="dm", single_use=True)`. Default single-use for DMs.
- Operator route: extend `operator_create_invite` (`POST /api/v1/groups/{gid}/invite`) with `?mode=dm|group`. `operator_revoke_invite` unchanged.

**Link:** `https://<funnel-origin>/app/#/g/<token>&k=<key>` — paste anywhere. Fragment holds `token`+`k`; the Funnel logs see only `/app/`.

**Handshake from the link:** §1.2 exactly. Guest generates ephemeral hybrid key, verifies operator `sig` under `ik_fp`, checks `bc`, runs PQXDH into `dm_ratchet`. `k` mixed into KDF so a bare URL-log leak can't complete the handshake.

**Join:** existing `POST /api/v1/guest/join` — extend body to `{invite_token, display_name, guest_pubkey, guest_kem_ct, guest_sig}`:
- `verify_group_invite(burn_single_use=True)` (unchanged burn).
- verify `guest_sig` over `canonical_sign_payload`-style `{jti, guest_pubkey, bc, ts}` so a **stolen link cannot be replayed by a third party who lacks the guest's key** (bind guest key to the invite).
- `add_untrusted_guest_member` → for `mode=dm`, cap the group at 2 seats (reject a 3rd guest_join → 403).
- return session JWT + LiveKit token + epoch-fenced bootstrap.

**Scoping:** §1.3. For DM, 2-seat cap + epoch fence + `sender=unverified` until SAS.

**Delivery:** Funnel :8443 (unchanged).

---

## 3. MODE B — Federated instance-to-instance

**Goal:** invite an agent/operator on a **federated** peer instance (tailnet S2S known). This is the high-trust, low-friction path.

**Invite:** operator issues a `create_pq_invite(..., mode="group"|"dm")` addressed to the peer's **FQID** (`<agent>@<operator>.<realm>`), `idm` = inviter capauth URI. Because the instances are federated, the token can travel as a **capauth-signed S2S envelope** to the peer's `/inbox` (no paste needed), but the same `/app/#/g/<token>&k=` link also works if the operator prefers to paste.

**Identity + PQ handshake:**
- Peer resolves inviter identity via capauth over the existing federation route; `fetch_peer_prekey` GETs the inviter's `/api/v1/prekey/<short>` and verifies against `bc` + operator `sig`.
- **Cross-signing borrow (Matrix):** once a peer *operator identity* is verified (SAS the first time, or already trusted in the capauth trust graph), its **agents/devices inherit trust** — no re-SAS per instance. Store this as a capauth trust edge; `require_signed_prekeys` stays ON for federated peers (fail-closed).
- Handshake is full RFC-0001 hybrid (not "untrusted guest") because both sides have durable identities → the joiner enters as a **verified MEMBER**, not an untrusted guest. Downgrade-locked once both advertise hybrid.

**Scoping:** normal member (role from the invite), full history per group policy. No guest fence.

**Delivery:** tailnet **S2S `/inbox`** + Nostr discovery for presence. This is the existing federation path; the only new bits are the `bc`/`ik_fp`/`sig` claims and the cross-sign trust inheritance.

---

## 4. MODE C — Non-federated cross-instance invite via the peer's identity (accept/sign)

**Goal:** invite a peer on **another skchat instance you have NOT federated with**, using only **their identity** (their `nprofile`-style blob or capauth pubkey). No shared server, no S2S handshake, must be MITM-proof and PQ-safe. This is the hardest case and the reason for the accept/sign step.

**Prereq — the peer's reachability blob (`nprofile` borrow):** the peer publishes a self-signed **reachability record** = `{ identity_pubkey (capauth/ML-DSA), instance_urls[], funnel_hint, dm_relay_hints[] (Nostr) }`. The operator obtains it out-of-band (pasted, QR, or via a common Nostr relay keyed by the peer pubkey — the NIP-19 `nprofile` / kind-10050 analog). This is the "I only have the peer's identity" input.

**Step 1 — operator issues an addressed, attenuated invite (cap-token):**
- `create_pq_invite(mode=…)` but with **macaroon-style caveats** baked into the signed claims: `aud = <peer_identity_fp>` (only this peer may accept), `once=true`, `exp`, `scope=dm|group`. The invite `bc` commits to the operator bundle. Operator signs under `ik_fp`. This collapses "who are you" + "what may you do" into one blob (Tahoe/macaroon borrow).

**Step 2 — deliver the invite to the peer (gift-wrap over Nostr/Funnel):**
- Wrap the invite in a **per-recipient sealed envelope** (NIP-59 gift-wrap borrow, mapped onto skcomms' capauth-signed envelope): inner = `{invite_token, sig, k, operator_reachability}` → sealed to the peer's `identity_pubkey` (hybrid x25519+ML-KEM to the peer's advertised bundle) → outer wrap signed by a **fresh throwaway key**, `p`-tag = peer only, randomized `created_at`. Publish to the peer's advertised `dm_relay_hints` (Nostr) or POST to its `funnel_hint`. The relay learns neither the operator identity nor the invite contents.

**Step 3 — peer reviews + builds an ACCEPT ASSERTION (the sign step):**
```
accept_assertion = {
  jti,                      // the invite being accepted
  guest_pubkey|peer_pubkey, // accepter's identity/ephemeral key
  bundle_hash: bc,          // echoes the operator commitment it verified
  peer_kem_ct,              // ML-KEM encapsulation to operator bundle
  ts
}
sig_peer = Sign(peer_identity_key, canonical(accept_assertion))   // capauth/ML-DSA
```
- The peer FIRST verifies the operator `sig` under `ik_fp` and `SHA256(bundle)==bc` (downgrade lock). If the operator identity is unknown, it **pins TOFU + shows SAS** for OOB comparison.
- The peer sends `accept_assertion + sig_peer` back to the operator, gift-wrapped the same way to the operator's reachability. Reuse `canonical_sign_payload` (extended to this shape) so the exact signed bytes are reproducible on both sides.

**Step 4 — operator counter-signs → mutual join record (self-authenticating membership proof):**
```
join_record = {
  invite_jti, operator_id, peer_id,
  operator_bundle_fp, peer_bundle_fp,
  accept_assertion, sig_peer,
  ts
}
sig_operator = Sign(operator_identity_key, canonical(join_record))
```
- Operator reviews (TOFU pin of the peer key + optional human confirm), burns the invite (`_mark_used(jti)`), counter-signs. **Both sides persist `join_record` + both sigs** — this mutual, peer-signed record IS the membership proof. No identity server touched. Mirrors Matrix third-party-invite ephemeral-key flow but with **zero trusted server**.
- With both sides' KEM cts exchanged, both derive the same RFC-0001 root key → E2EE session live; for a group, the counter-sign yields the peer's KeyPackage-equivalent and the operator returns the group seal (Welcome-analog) atomically.

**Scoping:** a mode-C peer joins as a **verified member of the addressed group only** (aud caveat enforces it). Not "untrusted guest" — it has a real, pinned identity. But until SAS is confirmed OOB, UI marks it `identity-pinned, unverified`.

**Delivery:** Nostr discovery relay (dumb rendezvous, gift-wrapped) with Funnel fallback. Zero trust in the relay: all authentication is in the token keys + the two signatures.

New module: `guest_accept.py` — `build_accept_assertion()`, `verify_accept_assertion()`, `build_join_record()`, `verify_join_record()`, `consumed_nonces` local revocation list (accept-list of burned `jti`, since bearer caps can't be un-shared).

---

## 5. Threat model

| Threat | Mode | Mitigation (grounded) |
|---|---|---|
| Relay/Funnel/CDN logs the joinable secret | all | Secret in **fragment** (`#/g/<token>&k`), never in path/query; server never sees it. Optional server-held encrypted link-data (§1.4). |
| Malicious/compromised daemon forges an invite | all | Invite carries **operator `sig` under `ik_fp`** (identity key), verified independent of the HS256 server secret. |
| PQXDH KEM-stripping downgrade | all | Signed **bundle commitment `bc`**; absent/classical bundle = verify failure, not silent fallback. `SKCHAT_REQUIRE_SIGNED_PREKEYS` fail-closed for B/C. |
| MITM swaps the link on the sharing channel (no directory) | A, C | **SAS / safety-number** shown in `guest_landing_screen`, compared OOB; Briar-style mutual-commit in mode C (both sign). |
| Stolen link replayed by a third party | A | Guest **binds its key to the invite** (`guest_sig` over `{jti,guest_pubkey,bc}`); link alone insufficient. |
| Leaked URL used twice | all | Single-use **burn** (`_mark_used` atomic race-loser reject); server-held link-data locks to first accepter. |
| Dangling/leaked reusable invite | all | Operator-gossiped **revocation** keyed by public `jti` (`_is_revoked`, `operator_revoke_invite`); local `consumed_nonces`. |
| Guest escalates (adds members, reads history, rotates keys) | A | Structural: MEMBER-not-ADMIN, `_deny_cross_group`, **epoch fence** on history, 2-seat cap for DM. |
| Relay correlates social graph / timing | C | **Gift-wrap** (fresh throwaway outer key, randomized `created_at`, per-recipient), bucket-padded; publish to overlapping common relays. |
| Sender/recipient metadata leak on relay | C | NIP-59 gift-wrap: real sender/kind/ts sealed inside; outer `p`-tag = recipient only. Never re-leak in the skcomms outer envelope. |
| OPK exhaustion → last-resort reuse weakens FS | all | Monitor/replenish one-time prekeys server-side (`pq_prekeys`); alert when low. |
| PQ-auth gap (classical signatures forgeable later) | B, C | Path to **ML-DSA composite identity** (`sk_pgp`, Sequoia PQC) for the operator/peer `sig`; capauth PGP interim. |
| No key recovery (guest loses key) | A | Ephemeral-by-design; explicit "your key lives only in this browser" warning; no false recovery promise. |
| Guest key reused across invites (linkable) | A | **Per-invite ephemeral** guest keypair; never reuse. |

Oracle hygiene: keep the existing generic 401/403 (`InviteInvalid`/`SessionInvalid` map to non-distinguishing errors; operator routes 404 when flag off).

---

## 6. Phased implementation plan (additive, flag-gated → coord tasks)

All phases gated by existing `SKCHAT_GUEST_LINKS_ENABLED` plus new `SKCHAT_PQ_INVITES_ENABLED` (default off) and reuse `SKCHAT_REQUIRE_SIGNED_PREKEYS`.

**Phase 0 — 1:1 as degenerate group (unblocks Mode A DM, no crypto change).**
- `guest_groups.create_dm_invite()` + 2-seat DM group (`metadata.mode="dm"`); `operator_create_invite` gains `?mode=`.
- `guest_join` enforces 2-seat cap for `mode=dm`; epoch fence in `_guest_messages`.
- Tests: dm invite → join → third join rejected; history fenced. *(smallest shippable slice; pure reuse.)*

**Phase 1 — signed invite + bundle commitment (closes downgrade + forge gaps).**
- Extend `create_group_invite`/`create_pq_invite` with `ik_fp, bc, idm, mode` claims + operator detached `sig` (reuse `prekey_sig` signing).
- `guest_invite_preview` returns `{idm, ik_fp, bc, mode, operator_sig}`; joiner verifies `sig` + `SHA256(bundle)==bc` before handshake.
- `guest_join` verifies `guest_sig` binding guest key to `{jti, guest_pubkey, bc}`.
- Flutter `guest_landing_screen`: bundle-fetch, commitment check, **SAS display**.
- Tests: bad `bc` → abort; forged sig → reject; replay w/o guest key → 401.

**Phase 2 — PQ handshake from the link into the ratchet (Mode A fully PQ).**
- Wire the guest ephemeral hybrid keypair → PQXDH → `dm_ratchet`, mixing fragment `k` into KDF `info`. First guest message E2EE.
- Add `k` to `join_url` (`/app/#/g/<token>&k=`); app splits fragment.
- Tests: `test_guest_pqxdh_first_message`, downgrade-lock, `k`-absent → handshake fails.

**Phase 3 — Mode C accept/sign (non-federated peer).**
- New `guest_accept.py`: `build/verify_accept_assertion`, `build/verify_join_record`, `consumed_nonces`.
- Reachability record publish/fetch (`nprofile` analog) + gift-wrap envelope over skcomms Nostr/Funnel (reuse capauth-signed envelope; add fresh-throwaway outer key + randomized ts).
- macaroon caveats (`aud`, `scope`) in invite claims + enforcement in `verify_group_invite`.
- Operator review/counter-sign UI (`features/join`); TOFU pin + SAS.
- Tests: full C round-trip, wrong-`aud` reject, MITM-swap caught by SAS, gift-wrap metadata hidden.

**Phase 4 — Mode B cross-sign trust inheritance + hardening.**
- capauth trust edge on verified peer operator → agents inherit (skip per-instance SAS).
- Server-held encrypted link-data (§1.4) + first-read lock.
- OPK replenish/alerting; `sk-alert` on low prekey pool.
- ML-DSA composite `sig` path when `sk_pgp` static-link blocker clears (tracked in `sk-pgp-library`).

**Dependency order:** 0 → 1 → 2 (Mode A done) → 3 (Mode C) → 4 (Mode B polish + hardening). Phases 0–2 ship Mode A end-to-end; each phase is independently flag-gated and reversible.

---

### Key file/function touchpoints for task authoring
- `src/skchat/guest_groups.py`: `create_group_invite` (+claims/sig), new `create_dm_invite`, `create_pq_invite`, `canonical_sign_payload` (extend to accept/join records).
- `src/skchat/guest_group_routes.py`: `operator_create_invite` (+`mode`), `guest_invite_preview` (+bundle/sig fields), `guest_join` (+`guest_kem_ct`/`guest_sig`, 2-seat cap, epoch fence).
- `src/skchat/pq_prekeys.py` / `prekey_sig.py` / `prekey_exchange.py`: bundle commitment hash, operator self-publish, fetch+verify against `bc`.
- `src/skchat/dm_ratchet.py`: guest ephemeral PQXDH entry with `k` in KDF info.
- new `src/skchat/guest_accept.py`: Mode C assertions + join record + consumed-nonce revocation.
- App: `features/guest/guest_landing_screen` (SAS, commitment check), `features/join/join_screen` (Mode C review/counter-sign), fragment split for `k`.
---

## 7. Adversarial-review hardening (folded in — MUST address)

A 4-lens red-team (security, sovereignty, guest-abuse, feasibility) found the following. These override the draft above where they conflict.

**C1 [CRITICAL, sovereignty] `idm` namespace + per-instance uniqueness.** `capauth:<agent>@skworld.io` is NOT unique per instance, so two sovereign operators collide and name-impersonation is trivial. **Fix:** `idm` MUST be the full FQID `<agent>@<operator>.<realm>` bound to the operator identity-key fingerprint; the display name is advisory only. Verification keys on the pubkey/fingerprint, never the name.

**C2 [CRITICAL, hidden directory] Ship the full operator public key inline, not just `ik_fp`.** A fingerprint can't verify a signature; needing the full key implies a lookup = a hidden central directory. **Fix:** the invite (or the `k`-decryptable server-held link-data, §1.4) carries the operator's FULL identity public key (capauth PGP now, ML-DSA composite later); `ik_fp` is only a display/comparison aid. Same for Mode C peer reachability records (self-contained `nprofile`, full key inline).

**H3 [HIGH, forward-secrecy] `bc` commits to the STABLE bundle portion only.** Committing to the whole bundle (incl. rotating one-time prekeys) either false-fails after OPK rotation or forces OPK reuse (breaks FS). **Fix:** `bc = SHA256(canonical(identity_key || signed_prekey))` — the long-lived, signed portion. OPKs are fetched fresh and verified by the SPK signature, not by `bc`.

**H4 [HIGH, blast radius] Pin where the operator identity key lives + scope cross-sign.** Mode B trust-inheritance amplifies a daemon compromise. **Fix:** the operator identity/signing key lives in the CapAuth Bunker (remote-signer / phone), NOT the daemon; the daemon holds only the HS256 server secret + prekeys. Cross-sign trust inheritance is opt-in per peer-operator and revocable, not automatic.

**H5 [HIGH, revocation] Identity pins + join_records are rotatable/revocable.** `join_record` trust must not be permanent. **Fix:** a local identity trust-store with rotate + revoke; a signed `revoke_pin` record; `consumed_nonces` is an accept-list (burned `jti`) that also carries pin revocations.

**H6 [HIGH, sovereignty leak] Be honest that Mode C uses a shared rendezvous relay.** "No directory" becomes "shared Nostr relay" for discovery. **Fix:** document it plainly; the relay is a DUMB, zero-trust, gift-wrapped rendezvous (sees only ciphertext + a throwaway key), and the operator can point at their OWN relay/Funnel. No trust is placed in it; it is availability only.

**H7 [HIGH, sequencing] The fragment secret `k` ships in Phase 1, not Phase 2.** Phases 0-1 as drafted would ship the exact URL-log leak the design claims to close. **Fix:** move `k` (fragment-only secret + KDF salt) into Phase 1 alongside the signed invite, so no phase ever ships a joinable secret in the path/query.

**M8 [MEDIUM, honest threat model] SAS is not a real defense for the anonymous Mode A guest.** A stranger who got the link over SMS has no independent OOB channel to compare a safety number. **Fix:** for anonymous Mode A, rely on single-use burn + guest-key binding + the operator vouching for the link's origin; present SAS as "compare with the person who sent you this if you can," not as a security guarantee. Reserve real SAS/verification for Mode B/C where both sides have durable identities.

**Net effect on the plan:** Phase 1 gains "ship full pubkey inline + `k` fragment secret + FQID `idm`"; `bc` scoped to identity+SPK; the Bunker holds the signing key; Mode C (Phase 3) documents the zero-trust relay + adds pin rotation/revocation. All still additive + flag-gated.
