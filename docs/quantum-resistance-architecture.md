# SK Ecosystem — Quantum-Resistance Architecture & Migration Plan

**Scope:** skchat, skcomms, capauth, cloud9 (and the shared crypto surfaces in sksecurity).
**Author:** Lead Security Architect
**Status:** Draft architecture + boardable plan. Designed to run **alongside** the comms-suite work (side-tabbable per Chef).
**Date:** 2026-06
**Standards anchor:** FIPS 203 (ML-KEM), FIPS 204 (ML-DSA), FIPS 205 (SLH-DSA); NIST CSWP 39 (crypto-agility); RFC 9580 + draft-ietf-openpgp-pqc-17 (OpenPGP PQC composites).

> **Framing in one sentence:** The urgent problem is **Harvest-Now-Decrypt-Later (HNDL)** against our *confidentiality* surfaces (recorded ciphertext is retroactively breakable once a CRQC exists). Signatures are **not** retroactively breakable, so identity/auth migration is real but deferrable. We act now on key exchange + key wrapping, not because Q-Day is 2026 — it is plausibly **early-to-mid 2030s** — but because Mosca's Inequality (data-shelf-life + migration-time > years-to-CRQC) is already breached for our longest-lived secrets.

---

## 0. The honest-claim statement (read this first)

This is the single most important section. Everything else exists to make these claims *true*.

### 0.1 What we CAN truthfully claim TODAY (pre-Phase-1)

- **"Our symmetric and at-rest layers are already quantum-resistant."** AES-256-GCM, ChaCha20-Poly1305, SHA-256/384, HKDF, scrypt are Grover-only (worst case half the bit-strength → still ≥128-bit). This is **true now** and requires no migration. (See §2.)
- **"Our edge-facing TLS to browsers negotiates hybrid post-quantum key exchange (X25519MLKEM768) where the client supports it."** True **only for the client↔Cloudflare-edge leg** — Cloudflare GA'd this. The CF↔origin leg and our own daemons are **not** PQ yet, so this claim must be scoped to "browser-to-edge," never end-to-end. (See §3, Cloudflare.)
- That is the **entire** list. Everything asymmetric we own — capauth identity, all PGP key-wrap, all SignedEnvelope signatures, group-key distribution, the tailnet handshake — is classical and Shor-breakable.

### 0.2 What we MAY claim AFTER Phase 1 (KEM/HNDL items shipped)

- **"skchat/skcomms message confidentiality is protected by hybrid post-quantum key encapsulation — X25519 + ML-KEM-768 (FIPS 203) — for transport and group-key distribution. A recorded ciphertext stays secret unless *both* X25519 and ML-KEM-768 are broken."**
  Claimable **only once** the group-key wrapping and the 1:1/envelope KEM are on the hybrid combiner AND a runtime self-report proves the negotiated suite per channel (see §4.4 inventory/self-report).
- **"Long-lived data at rest (memory stores, AI-LIFE content, snapshots, the capauth root backup) is wrapped with a hybrid post-quantum KEM, so harvested encrypted backups are not retroactively decryptable."**
  Claimable once the at-rest key-wrap layer (§5, Phase 1, Sprint Q4) is live.

### 0.3 What we may claim after Phase 2 (signatures/identity)

- **"Agent and operator identity is authenticated with hybrid post-quantum signatures (Ed25519 + ML-DSA-65, FIPS 204), with an SLH-DSA (FIPS 205) hash-based option for the sovereign root."**

### 0.4 What is ALWAYS OVERCLAIMING — never say these

- ❌ **"quantum-proof" / "unbreakable" / "quantum-safe encryption"** — no scheme is *proven* secure; lattice crypto is young. The defensible word is **"quantum-resistant"** or **"post-quantum"**, never "-proof."
- ❌ **"end-to-end quantum-resistant"** while any leg is classical (CF→origin, tailnet handshake, LiveKit DTLS, PGPy-signed envelopes). Scope every claim to the exact surface.
- ❌ **"PQC" when only signatures migrated** — that does nothing for HNDL. Signature-only PQC against the harvest threat is marketing.
- ❌ **"CNSA 2.0 compliant"** — we deliberately use the **-768 hybrid tier** (excellent practice), not the level-5 CNSA ceiling (ML-KEM-1024 / ML-DSA-87 / SHA-384-min). CNSA 2.0 is our *aspirational reference bar*, not our claim.
- ❌ **"FIPS 206 / Falcon"** — FIPS 206 (FN-DSA) is draft-stage; not claimable until ~late-2026/2027.
- ❌ **"our tailnet is quantum-safe"** — WireGuard has no crypto-agility; the Curve25519 Noise handshake is classical. (Tailscale's PSK/Rosenpass path is experimental and not on mobile.)
- ❌ Implying **AES-256 is "broken" by quantum** — it is not. Grover halves it to ~128-bit, which is safe. No fear-based AES messaging.

**Rule:** every external quantum-resistance claim MUST cite the surface + the FIPS number + hybrid-vs-classical + be backed by the runtime self-report (§4.4). No claim without evidence.

---

## 1. Threat model & priority (why this ordering)

| Driver | Consequence |
|---|---|
| **CRQC timeline** (GRI/evolutionQ 2025: 28–49% within 10 yr, "likely" within 15) | Plan for **2030s**, not 2026. Don't justify urgency with "Q-Day imminent" — that's not credible. |
| **HNDL** | Adversary records ciphertext **today**, decrypts after CRQC. Retroactive. This is the *real* present threat. |
| **Mosca's Inequality** `X(secrecy-life) + Y(migrate-time) > Z(years-to-CRQC)` | For 10–20-yr secrets (AI-LIFE content, agent memories, root key), X+Y already > Z. **Act now on those.** |
| **Signatures are not retroactive** | A forged future handshake sig can't decrypt a *past* session. Auth migration is real but **deferrable to Phase 2**. |
| **Symmetric is fine** | AES-256/SHA-2 only lose ½ bit-strength to Grover. **Do not touch.** |

**Priority order (by data-shelf-life, not system criticality):**

1. **Key exchange / KEM confidentiality** — group-key distribution, 1:1 DM wrap, envelope payload wrap. *(HNDL, urgent — Phase 1)*
2. **At-rest key-wrap for long-lived data** — memory stores, AI-LIFE content, snapshots, root-key backups. *(HNDL, urgent — Phase 1)*
3. **Identity ROOT keys** — capauth/PGP sovereign root, agent signing keys (long-lived, catastrophic blast radius). *(Phase 2)*
4. **Routine signatures** — per-message SignedEnvelope auth, challenge-response. *(Phase 2, after agility lands)*
5. **Transport/media** — tailnet handshake, CF→origin, LiveKit DTLS. *(Phase 3 — mostly external deps; ephemeral media is lowest HNDL value)*

---

## 2. What's already fine — DO NOT REDO

These are quantum-acceptable today. Touching them is wasted effort (and downgrading AES-256 would be a regression).

| Surface | Primitive | Verdict |
|---|---|---|
| skchat group **message** cipher | AES-256-GCM (`group.py:GroupMessageEncryptor`) | ✅ Grover-only ~128-bit. Keep. Only its *PGP key wrapper* is the problem (§3). |
| skchat at-rest store | HKDF-SHA256 + AES-256-GCM (`encrypted_store.py`) | ✅ Symmetric/hash. *(Separate classical bug: key derived from PGP **fingerprint** — low-entropy/public. Fix regardless of quantum — §3 note.)* |
| sksecurity KMS internal tree | scrypt + HKDF-SHA256 + AES-256-GCM, DEK=`os.urandom(32)` (`kms.py`) | ✅ Entirely symmetric/hash. **Caveat:** if a PGP key is ever wired as the master root, that root becomes vulnerable. |
| Bunker E2E symmetric layer | HKDF-SHA256 + AES-256-GCM (`bunker_e2e.py`) | ✅ (only the ephemeral X25519 step is asymmetric — low HNDL value, 5-min TTL). |
| skcomms content-integrity hash | SHA-256 (`signing.py:115`) | ✅ Grover-only. |
| WireGuard / SRTP / TLS **data** ciphers | ChaCha20-Poly1305, BLAKE2s, AES-GCM | ✅ Only the *handshakes* are broken, not the bulk ciphers. |
| `cloud9/quantum.py` | (misnamed emotional-resonance math) | ✅ Not crypto. No surface. |

**Standing rule:** AES-256-GCM stays everywhere. Verify no **AES-128** sneaks into DTLS-SRTP profiles if we ever control LiveKit cipher config.

---

## 3. Vulnerable surfaces + the fix per surface

For each: target algorithm, hybrid construction, library/integration path. **Universal combiner (never deviate):**

```
shared_key = HKDF-SHA256( X25519_shared_secret || ML-KEM-768_shared_secret,
                          info = "<context-label>" )
```
Concatenate-then-KDF. **Never XOR, never replace** classical with PQ. Secure if *either* primitive holds. This is exactly TLS `X25519MLKEM768` and Signal PQXDH.

| # | Surface (file) | Today | Quantum status | Target + hybrid | Library path |
|---|---|---|---|---|---|
| S1 | **capauth root identity key** (`pgpy_backend.py`, `gnupg_backend.py`; enum `models.py:24-25`) | Ed25519 / RSA-4096 | 🔴 Shor-broken. Highest-value long-lived secret; public key published in DID by design. | **Signing:** ML-DSA-65 + Ed25519 composite (OpenPGP alg 30) for agents; **SLH-DSA-SHAKE-256 standalone** for the rarely-rotated *sovereign root* (hash-only, no lattice assumption). **Encryption subkey:** ML-KEM-768 + X25519 composite (alg 35). | Migrate backend off **PGPy → Sequoia-PGP** (shipped PQC Nov 2025) or GopenPGP. Add `Algorithm.ML_DSA65_ED25519`, `Algorithm.SLH_DSA`, `Algorithm.ML_KEM768_X25519` to enum. **(Phase 2)** |
| S2 | **capauth challenge-response + DID sig** (`identity.py`, `did.py`, `login.py`) | Ed25519/RSA detached sig | 🔴 Future-forgery (not HNDL). | ML-DSA-65 + Ed25519 hybrid sig; verify either-or during transition. | Sequoia backend; alg-id in signed payload. **(Phase 2)** |
| S3 | **skcomms SignedEnvelope signature** (`signing.py:91-116`) | PGPy detached Ed25519/RSA over `canonical_bytes()` + SHA-256 hash | 🟡 Hash fine; **sig forgeable** post-Q for known pubkey | ML-DSA-65 + Ed25519 hybrid sig. Add `sig_alg` field to `SignedEnvelope`. | liboqs-python ML-DSA OR Sequoia. **(Phase 2)** |
| S4 | **skcomms envelope payload encryption** (`crypto.py:EnvelopeCrypto.encrypt_payload`) | PGP Curve25519/RSA wrap of AES-256 session | 🔴 **HNDL** — recorded ciphertext retroactively decryptable | **Hybrid KEM wrap:** `K = HKDF(X25519_ss ‖ MLKEM768_ss)`; AES-256-GCM bulk unchanged. | liboqs-python ML-KEM-768 + pyca X25519. **(Phase 1 — Q3)** |
| S5 | **skchat group-key DISTRIBUTION** (`group.py:652 GroupKeyDistributor`) | PGP-wrap of `os.urandom(32)` group key per member | 🔴 **HNDL + highest leverage** — break one member's classical key → recover AES group key → decrypt *all* group history | **Per-epoch** group secret distributed via hybrid KEM (not a static long-lived AES key). Replace static `group_key` with KDF-ratcheted **sender-key + epoch** model; re-key on add/remove OR ~50-msg/7-day bound. | liboqs-python ML-KEM-768 + X25519; new `group_ratchet.py`. **(Phase 1 — Q1+Q2, the marquee item)** |
| S6 | **skchat 1:1 DM crypto** (`crypto.py:ChatCrypto.encrypt_message/sign_message`) | PGP encrypt-to-recipient + PGP sig | 🔴 KEM = HNDL (Phase 1); sig = future-forgery (Phase 2) | Hybrid KEM for the wrap (Phase 1); hybrid sig (Phase 2). PQXDH-style: signed KEM-prekey in bundle, ~1 KB ciphertext in first message. | liboqs-python; reuse the §S4/S5 KEM helper. **(Phase 1 KEM / Phase 2 sig)** |
| S7 | **CapAuth Bunker E2E** (`bunker_e2e.py`) | Ephemeral X25519 → HKDF-SHA256 → AES-256-GCM, 5-min TTL | 🟡 Asymmetric **but ephemeral + short-lived → low HNDL value** | Hybrid X25519+ML-KEM-768 ephemeral KEX (cheap once helper exists). | pyca X25519 + liboqs-python. **(Phase 1 stretch / Phase 3 — low priority)** |
| S8 | **Transport: Tailscale/WireGuard** (external) | Curve25519 Noise handshake, no PSK | 🔴 HNDL on recorded tunnel traffic; **no in-WireGuard fix possible** | Document as external dep. Optionally feed a PQ-hybrid-derived **PSK** into the WireGuard PSK slot (Tailscale Rosenpass path — experimental, **no mobile**). | Out-of-repo. **(Phase 3, document-only)** |
| S9 | **Transport: Cloudflare TLS** (external) | Edge: hybrid X25519MLKEM768; **origin leg classical** | 🟡 Edge partial, **origin vulnerable** | Rebuild origins (the daemons' TLS terminators / reverse proxies) against **OpenSSL 3.5+** so X25519MLKEM768 negotiates CF→origin; enable PQ Cloudflare Tunnel. | OpenSSL 3.5 LTS (native FIPS 203/204/205). **(Phase 3 — near-drop-in)** |
| S10 | **Transport: LiveKit/WebRTC DTLS-SRTP** (external) | DTLS 1.2, ECDHE P-256; SRTP AES-GCM | 🔴 Handshake vulnerable; **but ephemeral media = low HNDL unless recorded** | DTLS 1.3 + ML-KEM (BoringSSL supports; gated behind `WebRtcPostQuantumKeyAgreement`, desktop-only, off by default). | Out-of-repo; track upstream. **(Phase 3 — lowest urgency)** |
| S11 | **Stored long-lived secrets** (skmem-pg dumps, memory flat files, Syncthing-replicated trees, root-key backup) | Various / classical wrap where any | 🔴 **HNDL — prime target** (decade-secrecy data) | Hybrid **ML-KEM-768 + X25519 key-wrap** layer over the DEK; AES-256-GCM bulk stays. Consider `age` 1.3+ (native ML-KEM-768 hybrid recipients) for file sealing. | liboqs-python OR `age` 1.3. **(Phase 1 — Q4)** |

> **Note on S-at-rest (encrypted_store.py):** its key is derived from the PGP **fingerprint** (low-entropy, often public) — a **classical** weakness independent of quantum. Fix by deriving from secret key material / a KMS DEK regardless of timeline. Folded into Phase 1 Sprint Q4.

### 3.1 The browser/Flutter PQC gap (called out because it constrains S4–S6)

- **WebCrypto has NO PQC API in any browser (2026).** The skchat PWA cannot get app-layer ML-KEM from the platform.
- **Native Flutter (Android/iOS/desktop): SOLVED** via FFI — `oqs` pub.dev package (binds liboqs) or `mlkem_native`. **You must ship the liboqs native binary per-platform** in CI (missing-binary at runtime is the #1 failure).
- **Web: NOT solved by the platform.** Options, in order of preference:
  1. **WASM build of liboqs / mlkem-native** vendored into the PWA — workable, but we own the audit risk.
  2. **Pure-Dart/JS ML-KEM** — more audit risk; avoid unless WASM is blocked.
  3. **Server-side KEM with capability-gated downgrade** — the web client advertises *no* PQ capability, talks to a daemon that performs the hybrid KEM on its behalf over a PQ-TLS channel. Honest, but the web leg's E2E PQ property is weaker — **must be disclosed in the claim**.
- **Decision needed from Chef (§7).** Until resolved, **native clients get full hybrid KEM; the web PWA is documented as a reduced-assurance leg.** No claim may imply the browser is E2E PQ.

---

## 4. Crypto-agility — make this the LAST forced migration

Per **NIST CSWP 39** (Dec 2025): modularity, policy-mechanism separation, a cryptographic inventory, an agility maturity model. The single highest-leverage architectural change is **swap-ability**, worth more than any one parameter choice.

### 4.1 Cipher-suite / version field on every envelope & key

Add a **machine-readable suite identifier** to all crypto containers so peers negotiate without a flag-day. We already have anchors to build on:

- `Envelope.version` exists (`envelope.py:22`, currently `"1"`).
- `SignedEnvelope` has `signature`/`content_hash` but **no `sig_alg`** — add it.
- capauth already has a `crypto_backend: CryptoBackendType` field (`models.py:68`) and a `CryptoBackend` ABC — **extend that pattern everywhere**.

**Add these fields (additive, back-compatible):**

```python
# skcomms/envelope.py — SignedEnvelope
sig_suite: str = "ed25519-pgp-v1"        # e.g. "mldsa65-ed25519-v2"

# skchat/group.py — GroupChat (alongside existing key_version)
kem_suite: str = "pgp-curve25519-v1"     # e.g. "x25519-mlkem768-v2"
epoch: int = 0                            # ratchet epoch (distinct from key_version)

# capauth/models.py — Algorithm enum (new values)
ML_KEM768_X25519 = "mlkem768-x25519"     # OpenPGP composite alg 35 (encryption)
ML_DSA65_ED25519 = "mldsa65-ed25519"     # OpenPGP composite alg 30 (signing)
SLH_DSA          = "slh-dsa-shake-256"   # root-of-trust hash-only signer
```

### 4.2 Suite registry (policy-mechanism separation)

A single machine-readable registry (`skcomms/crypto_suites.py` or a YAML profile) maps `suite_id → {kem, sig, kdf, aead, params}`. Algorithm choice is **config-driven, never hard-coded**. The next migration (HQC backup KEM, parameter bumps, a broken primitive) becomes a registry entry + a negotiation, not a flag-day.

### 4.3 Backend abstraction (modularity)

Route ALL sign/verify/encrypt/decrypt through one internal interface (extend capauth's `CryptoBackend` ABC across skcomms/signing.py, skcomms/crypto.py, skchat/crypto.py, skchat/group.py). Today these are hardwired to PGPy types (`PGPMessage`/`PGPSignature`/`PGPKey`). The abstraction lets hybrid + classical coexist during rollout.

### 4.4 Crypto inventory + runtime self-report (claim evidence)

- **Static inventory:** grep every repo (skcomms, skchat, capauth, sksecurity, skmemory) for `X25519|Ed25519|RSA|AES|Curve25519|PGP`; record surface → primitive. (Phase 0.)
- **Runtime self-report:** extend `skcapstone doctor` / `sksecurity status` to report, **per live channel**, the negotiated KEM / signature / cipher and **hybrid-vs-classical**, citing FIPS 203/204/205. This is what makes every claim in §0 *evidence-backed rather than asserted.*

### 4.5 Downgrade protection

PQ material is **optional until both peers advertise support** (signed capability flag, never an unauthenticated header), then **locks in for the session** (cannot downgrade mid-session). This is exactly how Signal shipped SPQR back-compatibly and prevents downgrade attacks.

---

## 5. Phased migration

Each phase: **goal · surfaces · library · acceptance · risk · claim unlocked.**

### Phase 0 — Agility foundation (prereq, ~1 sprint)
- **Goal:** crypto inventory + suite-id fields + backend abstraction + self-report skeleton. No new algorithms yet.
- **Surfaces:** all (instrumentation only).
- **Library:** none new.
- **Acceptance:** every envelope/key carries a suite id; `sksecurity status` reports per-channel primitives; inventory doc committed.
- **Risk:** low. Touches serialization — guard with golden-vector round-trip tests.
- **Claim unlocked:** *internal only* — "we can enumerate our crypto per surface." (Enables honest §0.1.)

### Phase 1 — HNDL-urgent confidentiality (the real win)
- **Goal:** hybrid KEM on key exchange + group-key wrapping + long-lived at-rest secrets.
- **Surfaces:** S5 (group-key dist), S4 (envelope payload), S6-KEM (DM wrap), S11 (at-rest), S7 (bunker, stretch).
- **Library:** **liboqs-python 0.14+** (ML-KEM-768) + **pyca/cryptography** (X25519); `oqs` Dart FFI for native skchat-app; **`age` 1.3** for file/secret sealing.
- **Acceptance:** group key, DM, envelope payload, and at-rest DEKs all wrapped with `HKDF(X25519 ‖ MLKEM768)`; group uses **per-epoch ratchet** not static key; self-report shows `x25519-mlkem768-v2` on those channels; interop with classical-only peers via negotiated downgrade; golden vectors + cross-impl (Python↔Dart) tests pass.
- **Risk:** medium-high. ML-KEM keys/ct ~1 KB each (33× ECDH) → use sparse ratchet + chunking for groups; never per-message naive inclusion. PGPy→liboqs migration is the lift. Web PWA gap (§3.1) must be resolved or scoped.
- **Claim unlocked:** **§0.2** — "hybrid PQ KEM (X25519+ML-KEM-768) protecting transport, group keys, and long-lived data at rest; secret unless both primitives break." *This is the claim worth making.*

### Phase 2 — PQC signatures / identity
- **Goal:** quantum-resistant authentication for identity root + agent signing + envelope sigs.
- **Surfaces:** S1 (root), S2 (challenge/DID), S3 (envelope sig), S6-sig (DM sig).
- **Library:** **Sequoia-PGP** (or GopenPGP) for OpenPGP composites; liboqs-python ML-DSA-65 / SLH-DSA for non-PGP paths.
- **Acceptance:** capauth issues **additive** composite subkeys (ML-DSA-65+Ed25519 alg 30; ML-KEM-768+X25519 alg 35) per draft-ietf-openpgp-pqc-17 **without removing classical keys**; root has an SLH-DSA option; classical-only verifiers still verify; rotation/migration path documented for the root.
- **Risk:** medium. OpenPGP PQC is pre-RFC (interop GnuPG/Sequoia/RNP improving, not guaranteed) → keep composites **additive/reversible**. ML-DSA sigs ~3.3 KB (50× Ed25519) — budget envelope/QR/Nostr payload sizes.
- **Claim unlocked:** **§0.3** — "hybrid PQ signatures (Ed25519+ML-DSA-65), SLH-DSA root option."

> **▶ Phase 2 PROGRESS — 2026-06-24 (.158): signing backend BUILT, root NOT yet rotated.** See §5.1 below for the verified state.

#### 5.1 Phase 2 progress — Sequoia PQC signing backend landed (additive-first; root still classical)

**Status: backend BUILT and wired into capauth; the live sovereign root is STILL CLASSICAL.** Phase 2's signing/identity prerequisite is solved at the *tooling* level — but no quantum-resistant key has been issued for the real root yet, and none will be until the gated rotation ceremony (§7 open decision #4 "Root-key rotation"; see the honesty caveats below).

**Backend decision (evidence-based) — why Sequoia, not GnuPG.**
- **GnuPG is DISQUALIFIED for this work.** Its post-quantum support is **encryption-only** (ML-KEM / Kyber key-wrap). It **cannot sign or certify** with ML-DSA or SLH-DSA. (Tracked at GnuPG dev 2.5.20; stable 2.6 not shipped.) A PQC *signing* root cannot be built on GnuPG today.
- **Sequoia `sq` is the only backend that can host a PQC signing root.** We built **`sq` 1.4.0-pqc.1** (sequoia-openpgp 2.2.0-pqc.1) from crates.io:
  ```
  cargo install sequoia-sq --version 1.4.0-pqc.1 --locked \
      --no-default-features --features crypto-openssl
  ```
  - **Toolchain:** rustc 1.96.0 via rustup (Sequoia needs ≥1.79; the system rustc 1.75 is too old — system rust left untouched).
  - **Crypto provider:** linuxbrew **OpenSSL 3.6.2** (native ML-KEM / ML-DSA / SLH-DSA). Build env: `OPENSSL_DIR=/home/linuxbrew/.linuxbrew/opt/openssl@3`, `BINDGEN_EXTRA_CLANG_ARGS="-I$OPENSSL_DIR/include"`, `PKG_CONFIG_PATH=$OPENSSL_DIR/lib/pkgconfig`, `CARGO_TARGET_DIR=~/pqc-build/target`. apt deps: `pkg-config capnproto clang libsqlite3-dev patchelf`.
  - **Durability:** `patchelf --set-rpath /home/linuxbrew/.linuxbrew/opt/openssl@3/lib ~/.cargo/bin/sq` so `sq` runs without `LD_LIBRARY_PATH`. Binary: `~/.cargo/bin/sq`. Build script `~/pqc-build/build-sq.sh`, log `~/pqc-build/build.log`.

**Verified `sq` PQC capability (generate → sign → verify, end-to-end).**
- `sq key generate` cipher-suites in this build: `mldsa65-ed25519`, `mldsa87-ed448` (plus classical `cv25519`, `rsa2k/3k/4k`). **No standalone SLH-DSA primary** is exposed by `sq` here — SLH-DSA (FIPS 205) exists only at the liboqs layer (liboqs 0.14.0 ships the full SLH-DSA family + ML-DSA-87 + ML-KEM-1024, no rebuild needed). This narrows the §3/S1 "SLH-DSA standalone root" option to a liboqs path, **not** an `sq`-native one.
- **Strongest standards root `sq` can emit = `mldsa87-ed448`** (requires `--profile rfc9580`, i.e. OpenPGP v6):
  - Primary: **ML-DSA-87 + Ed448** composite (FIPS 204, NIST L5; certify + sign + auth) — draft code point **31**.
  - Encryption subkey: **ML-KEM-1024 + X448** composite (FIPS 203, L5) — draft code point **36**.
  - v6/RFC 9580 fingerprints are **64 hex chars** (not 40).
- Note: `sq sign` has **no `--password` flag** — signing with a password-protected key must go through the `sq` keystore or another path (open item to investigate before the ceremony).

**capauth code already landed (capauth `main` @ `34dbcf0`).**
- `src/capauth/crypto/sequoia_backend.py` — **`SequoiaBackend`** implements the `CryptoBackend` ABC (`crypto/base.py`): `generate_keypair` / `sign` / `verify` / `fingerprint_from_armor`, driving the `sq` subprocess.
- `src/capauth/models.py` — new **`Algorithm.HYBRID_ED448_MLDSA87`** (`"hybrid-ed448-mldsa87"`) + suite id **`"mldsa87-ed448-v2"`**; new **`CryptoBackendType.SEQUOIA`**. `crypto/__init__.py`: `get_backend(SEQUOIA)` wired.
- `tests/test_sequoia_backend.py` — 4 TDD tests (keygen → ML-DSA-87, sign/verify + tamper, fingerprint round-trip, factory).
- Existing surfaces this slots beside: PGPy backend `crypto/pgpy_backend.py`; profile init `profile.py` (`init_profile → backend.generate_keypair`); challenge sign/verify `identity.py`; hybrid Q7 challenge `pqc_identity.py`; bunker remote-signer + DID `docs/CRYPTO_SPEC.md`, `did.py`.

> **Relation to the §5/§6 plan:** this is the **Q6 "Sequoia migration"** prerequisite delivered, plus the `mldsa87-ed448` (L5) option reserved for the sovereign root per §7 decision #2's recommendation ("reserve ML-KEM-1024 / SLH-DSA only for the sovereign root"). It does **not** yet deliver Q7 (hybrid sigs on live envelopes/DID/challenge) for the root, and it does **not** rotate the root.

**HONESTY CAVEATS (do not soften):**
- **The root identity is STILL CLASSICAL.** Chef's real sovereign root (fingerprint `02BC0EB3CAD31DB691A753C70C5629AB893F9746`) has **not** been migrated. Only the *capability to issue* a PQC root exists. No claim may state or imply the root is post-quantum yet.
- **Additive + reversible first.** The root-key rotation is a **deliberate, later, gated** step — a sovereign-trust ceremony performed with Chef's real key (§7.4). It is intentionally *not* done.
- **Pre-RFC.** We issue against **draft-ietf-openpgp-pqc-17** (Standards Track, in the RFC-Editor queue, **not yet an RFC**). Draft code points used: 30 ML-DSA-65+Ed25519, **31 ML-DSA-87+Ed448**, 32–34 SLH-DSA standalone, 35 ML-KEM-768+X25519, **36 ML-KEM-1024+X448**. These can change before the RFC; composites stay additive/reversible.
- **Per-surface scope still holds.** Already shipped *separately* from the root (Q7-adjacent, additive/opt-in): hybrid per-message + DID/challenge signatures — `skcomms.pqsig` (ML-DSA-65+Ed25519 composite) + `capauth.pqc_identity`; sksecurity ledger Entry #8. The **root** is a distinct surface and remains classical until the ceremony.
- Standards to cite, as always: FIPS 203 / 204 / 205 + RFC 8032 (Ed448) / RFC 9580 (OpenPGP v6). Forbidden as ever: "quantum-proof," unscoped "end-to-end," global/unconditional PQ, "CNSA-2.0."

### Phase 3 — Transport / media (mostly external deps)
- **Goal:** close transport legs we can, document the ones we can't.
- **Surfaces:** S9 (CF→origin via OpenSSL 3.5), S8 (tailnet — document + optional PSK), S10 (LiveKit DTLS — track upstream).
- **Library:** OpenSSL 3.5 LTS (native PQC); Tailscale Rosenpass (experimental); BoringSSL upstream.
- **Acceptance:** origins rebuilt on OpenSSL 3.5 negotiate X25519MLKEM768 (verify `openssl s_client -groups X25519MLKEM768`); tailnet + LiveKit limitations documented as out-of-repo deps in the self-report.
- **Risk:** low effort / mostly external. Test the LiveKit/coturn path before flipping PQC (larger ClientHello can trip old middleboxes).
- **Claim unlocked:** "browser-to-edge **and** edge-to-origin TLS is hybrid PQ" (scoped); honest documentation of tailnet/media residual classical legs.

---

## 6. Epic + boardable sprints

**Epic:** `PQC-MIGRATION` — *Quantum-resistance for the SK comms+identity stack (HNDL-first, hybrid, crypto-agile).*
Tag: `quantum-resistance`. Runs **alongside** the comms-suite epic; side-tabbable. Each sprint below is a coord task (goal / components / acceptance / deps / risk).

| Sprint | Goal | Components (named files/modules) | Acceptance | Deps | Risk |
|---|---|---|---|---|---|
| **Q0 — Inventory & agility scaffolding** *(Phase 0)* | Crypto inventory + suite-id fields + self-report skeleton | `skcomms/envelope.py` (+`sig_suite`), `skchat/group.py` (+`kem_suite`,`epoch`), `capauth/models.py` (Algorithm enum stubs), new `skcomms/crypto_suites.py`, extend `sksecurity status` | Inventory doc committed; every envelope/key carries suite id; self-report lists per-channel primitives; round-trip golden vectors pass | none | low (serialization) |
| **Q1 — Hybrid KEM helper + backend ABC** *(Phase 1)* | One vetted hybrid-KEM primitive + backend abstraction | new `skcomms/pqkem.py` (`hybrid_encap`/`hybrid_decap` = `HKDF(X25519‖MLKEM768)`), extend `CryptoBackend` ABC | liboqs-python wired; KATs vs known vectors; X25519+ML-KEM-768 round-trips; classical fallback path | Q0; liboqs-python install on .158/.41 | med (FO/decap-failure handling — use lib, never hand-roll) |
| **Q2 — Group epoch-ratchet (no static key)** *(Phase 1, marquee)* | Replace static group key with per-epoch KDF ratchet + hybrid-KEM distribution | new `skchat/group_ratchet.py`, refactor `skchat/group.py:GroupChat`/`GroupKeyDistributor`/`rotate_key`/`remove_member` | Per-epoch secret distributed via `pqkem`; re-key on add/remove + 50-msg/7-day bound; sparse/chunked PQ material (no 33× per-msg bloat); FS + PCS demonstrated; self-report `x25519-mlkem768-v2` | Q1 | high (bandwidth, ratchet correctness, loss/reorder) |
| **Q3 — Envelope + DM hybrid-KEM confidentiality** *(Phase 1)* | HNDL fix for skcomms payload + skchat 1:1 | `skcomms/crypto.py:EnvelopeCrypto`, `skchat/crypto.py:ChatCrypto.encrypt_message` | Payload/DM wrapped via `pqkem`; PQXDH-style signed KEM-prekey in bundle; negotiated downgrade with classical peers; interop tests | Q1 | med (prekey bundle + downgrade-lock) |
| **Q4 — At-rest hybrid key-wrap + fingerprint-keying fix** *(Phase 1)* | Protect long-lived data at rest; fix low-entropy keying | `skchat/encrypted_store.py` (derive DEK from secret material / KMS, not fingerprint), new at-rest wrap layer (or `age` 1.3 recipients) over skmem-pg dumps / memory trees / root-key backup | DEKs wrapped with hybrid KEM; at-rest keying no longer fingerprint-derived; restore round-trip verified | Q1 | med (data migration / re-wrap of existing stores) |
| **Q5 — Native Flutter PQC (oqs FFI) + web gap decision** *(Phase 1)* | skchat-app native clients do real hybrid KEM; web leg scoped | `skchat-app` FFI integration (`oqs`/`mlkem_native`), per-platform liboqs in CI; web PWA strategy per Chef decision (§7) | Native Android/iOS/desktop perform hybrid KEM (binary shipped per-arch); Python↔Dart cross-impl vectors match; web leg documented as reduced-assurance OR WASM-liboqs landed | Q1, Q3; **Chef decision** | high (per-platform binaries; browser audit risk) |
| **Q6 — Sequoia migration + composite identity subkeys** *(Phase 2)* | Move off PGPy; issue additive PQC subkeys | swap PGPy→Sequoia in `capauth/.../pgpy_backend.py` path, `skcomms/signing.py`, `skchat/crypto.py`; capauth issues alg-30/alg-35 composites | Composite subkeys issued additively; classical verifiers still pass; no classical key removed; rotation path documented | Q1 (agility) | high (PGPy is a dead end; pre-RFC interop) |
| **Q7 — Hybrid signatures on envelopes + DID/challenge** *(Phase 2)* | Quantum-resistant auth, crypto-agile | `skcomms/signing.py` (`sig_suite` → ML-DSA-65+Ed25519), `capauth/identity.py`/`did.py`/`login.py` | Envelopes/DID/challenge carry hybrid sig; either-or verify during transition; payload-size budget validated (QR/Nostr) | Q6 | med (sig size 50× Ed25519) |
| **Q8 — OpenSSL 3.5 origins + CF/tailnet/LiveKit doc** *(Phase 3)* | Close CF→origin TLS; document residual classical legs | rebuild daemon TLS terminators on OpenSSL 3.5; PQ CF Tunnel; self-report transport section | `openssl s_client -groups X25519MLKEM768` confirms CF→origin; tailnet/LiveKit residual documented; no E2E overclaim | Q0 (self-report) | low (mostly external) |
| **Q9 — Claim audit + runtime self-report GA** *(cross-cutting)* | Make every §0 claim evidence-backed | finalize `sksecurity status` PQC self-report; claim-language review of all docs/marketing | Every external claim maps to a self-report line citing FIPS 203/204/205 + hybrid-vs-classical; overclaim scan clean | Q2–Q4 (Phase 1), Q8 | low |

**Minimal viable PQ posture = Q0+Q1+Q2+Q3+Q4** (Phase 0+1). That unlocks the *real* claim (§0.2) and neutralizes HNDL on everything we own. Q5 extends it to native mobile. Phases 2–3 follow without urgency.

---

## 7. Open decisions for Chef

1. **Browser/Flutter PQC gap (the big one).** WebCrypto has no PQC. Pick the web-PWA strategy:
   - **(a) Vendor a WASM liboqs/mlkem-native build** — full hybrid KEM in the browser, *we own the audit risk*.
   - **(b) Server-side KEM with capability-gated downgrade** — web client advertises no PQ, daemon does the KEM over PQ-TLS; honest but the web leg is reduced-assurance and **must be disclosed**.
   - **(c) Native-only PQ** — skchat-app (Android/iOS/desktop) gets full hybrid KEM; the PWA stays classical-on-the-app-layer and we **never claim the browser is E2E PQ**.
   - *Recommendation:* **(c) now, (a) later.** Ship native hybrid KEM in Phase 1; treat WASM as a Phase-1.5 stretch once a vetted build exists.

2. **How aggressive — 768 hybrid vs CNSA-2.0 ceiling?** Default plan uses the **-768 hybrid tier** (ML-KEM-768 / ML-DSA-65), the internet default — *not* CNSA 2.0 (which needs ML-KEM-1024 / ML-DSA-87 / SHA-384-min). Going to the ceiling buys a stronger "maximum-resistance" claim at ~30–50% size/perf cost. *Recommendation:* **-768 hybrid for comms; reserve ML-KEM-1024 / SLH-DSA only for the sovereign root** (where size/perf is irrelevant and blast radius is catastrophic).

3. **PGPy → Sequoia migration scope/timing.** PGPy 0.6.0 is a **dead end for PQC** (no ML-KEM/ML-DSA roadmap) and blocks all PGP-surface remediation. Sequoia is the realistic target but is the **single largest engineering lift**. Decide: do it in Phase 2 (signatures), or pull it earlier if we want PGP-native hybrid KEM instead of liboqs for the wrap?

4. **Root-key rotation.** Phase 2 means generating a quantum-resistant root (SLH-DSA or hybrid) and a rotation/cross-sign ceremony. This is a **sovereign-trust event** — needs Chef's real root key and a planned ritual. When?

5. **OpenPGP PQC is pre-RFC.** draft-ietf-openpgp-pqc-17 is in the RFC-Editor queue; interop (GnuPG/Sequoia/RNP) is early. Comfortable issuing **additive, reversible** composite subkeys now, or wait for the RFC number?

6. **Priority vs comms-suite.** This epic is designed to run alongside and is side-tabbable. Confirm the parallel cadence — or front-load **Q0–Q4** if HNDL on AI-LIFE content is the top concern.

---

## Appendix — primitive cheat-sheet (for the claim, never overstate)

- **KEM:** ML-KEM-768 (FIPS 203) — pk ~1184 B, ct ~1088 B, ~33× X25519. Use ML-KEM-1024 only for the root.
- **Sig:** ML-DSA-65 (FIPS 204) — pk ~1952 B, sig ~3309 B, ~50× Ed25519, signing fast. SLH-DSA (FIPS 205) — hash-only, large/slow, **root-of-trust only**.
- **Symmetric (keep):** AES-256-GCM, SHA-256/384, HKDF, scrypt — Grover-only, quantum-acceptable.
- **Hybrid combiner (always):** `HKDF-SHA256( X25519_ss ‖ MLKEM768_ss, info=context )`. Never XOR, never pure-PQ.
- **Forbidden words:** "quantum-proof," "unbreakable," "end-to-end quantum-resistant" (unscoped), "CNSA-2.0 compliant" (we're -768), "FIPS 206/Falcon" (draft).
