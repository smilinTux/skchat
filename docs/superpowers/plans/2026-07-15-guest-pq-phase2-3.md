# Guest PQ Handshake (Phase 2) + Mode C Accept/Sign (Phase 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the first guest message end-to-end post-quantum encrypted (Phase 2), then add the operator counter-sign / accept flow for a non-federated peer that already has an identity (Phase 3), both additive and flag-gated.

**Architecture:** A `mode=dm` guest invite is a degenerate 2-seat group (Phase 0, shipped). Phase 1 (shipped, E2E-proven) added the operator-signed invite, bundle commitment `bc`, and the guest ECDSA key-binding `guest_sig`. Phase 2 wires the guest's ephemeral hybrid keypair through a PQXDH into `dm_ratchet` so `mode=dm` guest messages are sealed `pqdm1:` blobs (reusing `PqDmCodec` which is already byte-for-byte interop with the Python daemon for regular DMs). Phase 3 turns the existing `guest_accept.py` assertions (`build/verify_accept_assertion`, `peer_kem_ct`, `consumed_nonces`) into a live operator review + counter-sign UI with a TOFU pin and SAS.

**Tech Stack:** Python (FastAPI webui, `cryptography`, `sk_pqc`/liboqs), Dart/Flutter web (`sk_pqc` = `@noble/post-quantum` via JS-interop, `cryptography`, `pointycastle`). Build on .158, heavy builds on .41, browser-drive E2E via CDP on .41 (see memory `skchat-cdp-e2e-loop`).

## Global Constraints

- **Additive + flag-gated.** Everything new sits behind `SKCHAT_PQ_INVITES_ENABLED` (already ON via `10-pq-invites.conf`). No behavior change when off. Instantly reversible.
- **Interop constants (pin exactly, both sides):**
  - Hybrid KEM suite id: `x25519-mlkem768` (`PqDmCodec.hybridSuite` / `pqdm.HYBRID_SUITE` / `pqkem.SUITE_ID`).
  - Hybrid-sealed token wire scheme: `pqdm1:x25519-mlkem768:<base64(sealed)>`.
  - KEM ciphertext length: 1120 bytes (`pqkem.CIPHERTEXT_LEN`); hybrid public key: 1216 bytes, transported as hex.
  - HKDF combiner `info`: `sk_pqc/x25519-mlkem768/v1` (identical both sides; do NOT alter for guests unless the server can recover the same salt).
  - Canonical bytes (all signatures/commitments): `json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")`; Dart `jsonEncode` with keys inserted alphabetically reproduces this.
  - `bc = b64u(SHA256(canonical({"identity_key": ik, "signed_prekey": spk})))` — stable portion only (H3); OPKs excluded.
  - Guest binding (Phase 1, shipped): ECDSA-P256-SHA256 over `canonical({bc, guest_pubkey, jti})`, WebCrypto raw r‖s (64B) or DER.
- **Fail-closed (§5 oracle hygiene):** a missing/invalid commitment, signature, KEM ct, or an all-classical bundle is an ABORT, never a silent classical fallback. Generic 401 to the guest (no oracle).
- **Deploy:** `flutter build web --release --base-href /app/` on .158, then `rsync -a --delete skchat-app/build/web/ skchat/src/skchat/static/app/`; commit the bundle. The webui serves `/app` from `static/app` (NOT `build/web`). Verify `curl localhost:8765/app/.last_build_id` matches.
- **Writing style:** no em/en dashes anywhere (code comments, commit messages, docs). Commas, parentheses, colons, or new sentences.

---

## PHASE 2 — PQ handshake from the link into the ratchet (Mode A fully PQ)

### Task 1: Server exposes the operator's hybrid prekey to the guest, bc-verifiable

**Files:**
- Modify: `src/skchat/guest_group_routes.py:200-223` (`guest_invite_preview` response)
- Modify: `src/skchat/pq_invites.py` (add `operator_signed_prekey(crypto)` helper if not present)
- Test: `tests/test_guest_pq_handshake.py` (new)

**Interfaces:**
- Produces: `guest_invite_preview` response gains `"signed_prekey": "<hybrid_public_hex 1216B hex>"` when `pq_invites_enabled()`. The guest verifies `PQI.verify_commitment(identity_key, signed_prekey, bc)` before use.
- Consumes: `pq_prekeys` operator self-bundle (`identity_key`, `hybrid_public_hex`). `full_pubkey` (operator PGP identity key, already returned) supplies `identity_key` for the commitment recompute.

- [ ] **Step 1: Write the failing test** — preview returns `signed_prekey`, and `verify_commitment(full_pubkey_identity, signed_prekey, bc)` is True.

```python
def test_preview_exposes_bc_verifiable_signed_prekey(client_pq_enabled):
    tok = mint_dm_invite(client_pq_enabled)
    pv = client_pq_enabled.get(f"/api/v1/guest/invite/{tok}").json()
    assert pv["signed_prekey"]  # 1216-byte hybrid pub as hex
    from skchat import pq_invites as PQI
    assert PQI.verify_commitment(pv["ik_identity_key"], pv["signed_prekey"], pv["bc"]) is True
```

- [ ] **Step 2: Run it, verify it fails** — `pytest tests/test_guest_pq_handshake.py::test_preview_exposes_bc_verifiable_signed_prekey -v` → KeyError `signed_prekey`.
- [ ] **Step 3: Implement** — in `guest_invite_preview`, when `pq_invites_enabled()`, add `"signed_prekey": info.get("signed_prekey")` (and whatever field name `create_pq_invite` already stashes the operator's `hybrid_public_hex` under; trace `create_pq_invite`/`pq_invites` at execution time and reuse it, do not recompute). Ensure `bc` was built from this exact `signed_prekey`.
- [ ] **Step 4: Run tests** → PASS.
- [ ] **Step 5: Commit** — `feat(guest): expose bc-verifiable operator signed_prekey in invite preview`.

### Task 2: Server accepts the guest's hybrid prekey + KEM ct on join, wires `mode=dm` messages through `dm_ratchet`

**Files:**
- Modify: `src/skchat/guest_group_routes.py` (`guest_join` accept `guest_hybrid_pub` + `guest_kem_ct`; `guest_send`/`_guest_messages` seal/open for `mode=dm`)
- Modify: `src/skchat/dm_ratchet.py` (reuse `wrap_dm_epoch_secret`/`unwrap_dm_epoch_secret`; add a `guest_pqxdh_root(operator_priv, guest_hybrid_pub, guest_kem_ct, k)` entry that mixes fragment `k`)
- Modify: `src/skchat/guest_groups.py` (persist `k` keyed by `jti` at mint so the operator can recover it; store guest `hybrid_pub` on the member record)
- Test: `tests/test_guest_pq_handshake.py`

**Interfaces:**
- Consumes: `guest_join` body gains `guest_hybrid_pub` (1216B hex) + `guest_kem_ct` (1120B, base64) alongside the shipped `guest_pubkey`/`guest_sig`.
- Produces: a per-guest `DmRatchet` root secret = HKDF over (`unwrap_dm_epoch_secret(guest_kem_ct, operator_hybrid_priv)` ‖ `k`), `info` includes `jti`. Guest-group messages for `mode=dm` are stored/served as `pqdm1:` tokens sealed with the epoch message key.
- **DECISION to resolve in-situ:** whether to reuse `PqDmCodec`/`pqdm` sealing (symmetric AEAD under the derived key) vs the group SEAL path. Prefer the DM path (`dm_ratchet` message key → AEAD) since `mode=dm` is 1:1; trace `_guest_messages` + the group send handler before wiring.

- [ ] **Step 1: Failing test** — full server-side round trip: operator opens a guest-sealed message.

```python
def test_guest_pqxdh_first_message(pq_enabled):
    # guest generates hybrid kp, encapsulates to operator signed_prekey, derives root,
    # seals "hello" -> pqdm1 token; server (operator priv + stored k) opens it == "hello"
    root_g = guest_derive_root(op_signed_prekey_pub, kem_ct, k, jti)
    token = seal_pqdm(root_g, "hello")
    assert server_open_guest_message(jti, token) == "hello"
```

- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** — persist `k` at mint (keyed by `jti`, in the existing guest store under `_store_lock`); `guest_join` stores `guest_hybrid_pub` + derives+persists the ratchet root from `guest_kem_ct`; `_guest_messages`/`guest_send` seal/open with the derived key for `mode=dm`. Fail-closed if `guest_kem_ct` absent when `pq_invites_enabled()`.
- [ ] **Step 4: Run tests** → PASS; also assert `k`-absent → handshake derive raises (downgrade-lock).
- [ ] **Step 5: Commit** — `feat(guest): PQXDH root + dm_ratchet-sealed mode=dm guest messages`.

### Task 3: App runs the guest PQXDH and seals `mode=dm` messages; splits fragment `k`

**Files:**
- Modify: `lib/services/guest_group_service.dart` (join sends `guest_hybrid_pub`+`guest_kem_ct`; `send`/`conversation` seal/open `pqdm1:`; accept `signedPrekey`+`bc`+`k`)
- Modify: `lib/features/guest/guest_landing_screen.dart` (parse `&k=` from the route fragment; pass `signed_prekey`/`k` through)
- Modify: `lib/core/router/app_router.dart` (guest route already carries the token; extend to keep the `&k=` fragment)
- Reuse: `lib/services/pq_dm_codec.dart` (`PqDmCodec`), `lib/services/pq_prekey_service.dart` (hybrid keypair gen + `hybridPublic()`)
- Test: `test/guest_pq_handshake_test.dart` (new)

**Interfaces:**
- Consumes: preview `signed_prekey` (hex), `bc`, and `k` (fragment). `PqDmCodec` hybrid encap to `signed_prekey`; `PqPrekeyService.ensureKeyPair()` for the guest hybrid key.
- Produces: `join()` body gains `guest_hybrid_pub` (hex of `hybridPublic()`) + `guest_kem_ct` (base64 of the encapsulation). `send()` seals body to `pqdm1:` before POST; `conversation()` opens `pqdm1:` tokens.

- [ ] **Step 1: Failing Dart test** — the app-built canonical + encap round-trips against a Python `unwrap` fixture (or assert the token shape `pqdm1:x25519-mlkem768:` + 1120B ct length). Verify `bc`-check aborts on a swapped prekey.
- [ ] **Step 2: Run, verify fail.** `flutter test test/guest_pq_handshake_test.dart`.
- [ ] **Step 3: Implement** — before sealing, verify `sha256Base64Url(canonical({identity_key, signed_prekey})) == bc`, ABORT on mismatch (no classical fallback). Generate guest hybrid key, encapsulate to `signed_prekey`, send `guest_kem_ct`+`guest_hybrid_pub` on join; seal outgoing via `PqDmCodec`; open incoming. Split `&k=` from the fragment in the router/landing.
- [ ] **Step 4: Run tests** → PASS.
- [ ] **Step 5: Commit** — `feat(guest): app-side PQXDH + pqdm1 sealing for mode=dm invites`.

### Task 4: Phase 2 E2E on the live stack

- [ ] **Step 1:** Build (`--base-href /app/`) on .158, rsync to `static/app`, commit bundle, verify `.last_build_id` on the funnel.
- [ ] **Step 2:** Mint a `mode=dm` invite (carries `k`), drive the guest join + first message via CDP on .41 (`scratchpad/cdp_diag.py`, fresh `/tmp/cdp-profile`).
- [ ] **Step 3:** Assert the stored guest message is a `pqdm1:` token (not plaintext) via `_guest_messages`, and the operator daemon opens it to the sent text. Confirm `k`-absent link → join/handshake fails (downgrade-lock).
- [ ] **Step 4:** De-risk with a Python replica (`scratchpad/replica_pqxdh.py`, mirrors `replica_join.py`) that encapsulates to the operator prekey and confirms the server opens it, isolating app-vs-server if the drive fails.
- [ ] **Step 5:** Update memory `skchat-cdp-e2e-loop` + `skchat-security-hardening-flags`; commit.

---

## PHASE 3 — Mode C accept/sign (non-federated peer with an identity)

### Task 5: Macaroon caveats (`aud`, `scope`) in invite claims + enforcement

**Files:**
- Modify: `src/skchat/guest_groups.py` (`create_group_invite`/`create_pq_invite` accept `aud`/`scope`), `verify_group_invite` (enforce)
- Test: `tests/test_guest_accept.py` (extend)

- [ ] **Step 1: Failing test** — a wrong-`aud` invite is rejected 401; correct `aud` passes.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** — add `aud`/`scope` to the JWT `require` list and match on verify; fail-closed generic 401.
- [ ] **Step 4: Tests** → PASS.
- [ ] **Step 5: Commit** — `feat(guest): macaroon aud/scope caveats on invites`.

### Task 6: Operator review + counter-sign UI (Mode C), TOFU pin + SAS

**Files:**
- Create: `lib/features/join/join_screen.dart` (operator sees an inbound accept assertion, verifies SAS, counter-signs)
- Create: `lib/features/join/identity_trust_store.dart` (local TOFU pin store with rotate/revoke, H5)
- Modify: `lib/services/guest_group_service.dart` or new `lib/services/join_service.dart` (fetch pending accept assertions, submit `join_record`)
- Modify: `src/skchat/guest_group_routes.py` (routes: list pending accept assertions, operator counter-sign → `guest_accept.build_join_record`, publish)
- Reuse: `src/skchat/guest_accept.py` (`verify_accept_assertion`, `build_join_record`, `consumed_nonces`), `src/skchat/guest_giftwrap.py` (`seal/open_giftwrap`)
- Test: `tests/test_guest_accept.py` (full C round-trip), `test/join_screen_test.dart`

**Interfaces:**
- Consumes: `guest_accept.verify_accept_assertion(assertion)` → the peer's `{jti, peer_pubkey, bc, peer_kem_ct, ts}`; SAS derived from `bc` + both pubkeys.
- Produces: `build_join_record(...)` counter-signed by the operator; `consumed_nonces` burns the `jti`; a `revoke_pin` path (H5).

- [ ] **Step 1: Failing test** — full Mode C round trip: peer builds accept assertion → operator verifies → SAS matches → counter-signs join_record → nonce consumed; a MITM-swapped pubkey makes SAS mismatch; wrong-`aud` rejected; gift-wrap hides metadata.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement server routes + Dart UI** (SAS display as emoji/number sequence from a hash of `bc`‖peer_pub‖op_pub; TOFU pin persisted; counter-sign posts `join_record`).
- [ ] **Step 4: Tests** → PASS (`pytest tests/test_guest_accept.py`, `flutter test`).
- [ ] **Step 5: Commit** — `feat(join): Mode C operator counter-sign UI + TOFU pin + SAS`.

### Task 7: Phase 3 E2E + docs

- [ ] **Step 1:** Build + deploy the app (join_screen), drive the operator counter-sign path via CDP.
- [ ] **Step 2:** Exercise the zero-trust gift-wrap rendezvous (relay sees only ciphertext + throwaway key); confirm metadata hidden.
- [ ] **Step 3:** Update `docs/2026-07-15-sovereign-invite-join-architecture.md` status (Phases 2-3 shipped), memory, coord board.

---

## Self-Review notes
- Spec coverage: Phase 2 tasks 1-4 cover the roadmap's "wire guest ephemeral hybrid → PQXDH → dm_ratchet, mix `k`, add `k` to join_url, app splits fragment, downgrade-lock + k-absent tests." Phase 3 tasks 5-7 cover "guest_accept.py (exists), macaroon caveats, operator counter-sign UI, TOFU pin + SAS, gift-wrap, full C round-trip tests."
- Type consistency: `signed_prekey` = 1216B hybrid pub as hex everywhere; `guest_kem_ct` = 1120B base64 everywhere; canonical recipe identical app/server.
- Known in-situ decisions (flagged above, resolve while implementing, do NOT guess): (a) exact field name `create_pq_invite` uses for the operator hybrid prekey; (b) DM-message-path vs group-SEAL path for `mode=dm` sealing; (c) where `k` is persisted at mint. Each is a trace-then-implement, not a placeholder in the shipped code.
