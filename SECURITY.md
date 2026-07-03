# Security Policy — skchat

skchat is a **crypto component** of the [SKWorld](https://skworld.io) sovereign ecosystem:
it generates, exchanges, wraps, signs, and stores key material (device prekeys, group keys,
1:1 DM epoch secrets, at-rest DEKs, and capauth-signed mesh frames). It is a
**confidentiality surface**, so it carries a hard quantum-resistance requirement. This
document is the "how do I report a vulnerability, and what has this crypto actually been
through" front door, per
[sk-standards `SECURITY_DISCLOSURE_STANDARD.md`](https://github.com/smilinTux/sk-standards/blob/main/standards/SECURITY_DISCLOSURE_STANDARD.md).

## Reporting a vulnerability

- **Primary:** GitHub **private vulnerability reporting** — `Security ▸ Report a
  vulnerability` on [smilinTux/skchat](https://github.com/smilinTux/skchat/security).
  Keeps the report, the fix, and the advisory (GHSA) in one place.
- **Secondary (out-of-band):** if GitHub is unavailable or you prefer email, contact the
  smilinTux maintainers and **encrypt the report to the project PGP key** (fingerprint
  published on the repo's Security page / `skworld.io`). Do not post details in a public
  issue.
- **Acknowledgement SLA:** we acknowledge within **72 hours** and coordinate a disclosure
  date (default ≤ 90 days) under ISO/IEC 29147 / 30111 coordinated disclosure.
- **Safe harbour:** good-faith research under coordinated disclosure will not be pursued.

Supported version: the latest tagged release on `main`. Fixes land there first.

## Threat model

**In scope (what skchat defends):**

- **Message confidentiality at rest & in the key-wrap.** Bulk ciphers are AES-256-GCM
  (`group.py:GroupMessageEncryptor`) and HKDF-SHA256 + AES-256-GCM at rest
  (`encrypted_store.py`) — symmetric, Grover-only, quantum-acceptable. The key-wrap /
  key-distribution is the migrating leg: hybrid X25519+ML-KEM-768 (FIPS 203) on the
  surfaces skchat owns (1:1 DM ratchet, newly-created groups, at-rest DEK). See `SOP.md §9`.
- **Harvest-Now-Decrypt-Later (HNDL).** Neutralised on the T2 confidentiality surfaces by
  the hybrid KEM (secure if *either* the classical or the PQ leg holds) with per-epoch
  ratcheting (forward secrecy + post-compromise security).
- **Source authentication / anti-spoof.** Every SKGlossa mesh frame is capauth-signed with
  the sender's **own per-agent key**; `GlossaMeshGatekeeper.unwrap_inbound` rejects
  unsigned, tampered, or forged-source frames (claimed source FQID must equal the signing
  identity) before they reach advocacy/memory. Chat envelopes are PGP-signed via skcomms.
- **Downgrade safety.** A peer lacking hybrid/`pqdr1` capability stays on the prior
  classical path — never an undecryptable frame — and the gap is surfaced by the
  self-report (`skchat pqc report`), never silently claimed as secure.

**Out of scope / documented residual risk (NOT defended, do not claim otherwise):**

- **Signatures are classical.** Identity, envelope, and glossa source-auth use Ed25519/RSA
  via capauth/PGP — **not** hybrid ML-DSA (Phase 2). Signatures are not retroactively
  breakable, so this is deferrable, but it is **not** post-quantum today.
- **Transport legs are classical.** LiveKit **DTLS-SRTP** (voice/video/data-channel media)
  and the Funnel / CF→origin **TLS** legs are classical. **No end-to-end quantum-resistant
  claim** may be made across these legs.
- **SKGlossa mesh/codec is not confidentiality crypto.** The L0-L2 density ladder and
  weakest-peer-caps negotiation carry no key material and provide no encryption; glossa
  confidentiality rides the (classical) LiveKit DTLS leg. Only the gatekeeper's classical
  *signature* protects it.
- **Browser / Flutter-web leg.** WebCrypto has **no PQC API** in any 2026 browser — native
  clients get full hybrid via liboqs FFI, but the web PWA is a **documented
  reduced-assurance leg** (`docs/crypto-architecture.md §7`). No web client may claim E2E PQ.
- **Legacy groups.** Groups created before the hybrid default, or with a member lacking a
  hybrid prekey, remain classical (`kem_suite="rsa-pgp-wrap-v1"`) until migrated via
  `skchat pqc migrate-fleet`.
- Compromise of the operator/agent host, the capauth signing key, or the LLM backend is out
  of scope; so are third-party dependency CVEs (report upstream — we track and bump).

## Self-report (claim evidence)

Every quantum-resistance claim MUST cite **surface + FIPS number + hybrid-vs-classical**,
backed by a runtime self-report — no claim without evidence.

```bash
skchat pqc report          # honest per-surface PQC report (delegates to sksecurity.pqc_report)
```

Per-object views: `GroupChat.crypto_self_report()`, `EncryptedChatHistory.crypto_self_report()`.
**Forbidden claims:** "quantum-proof" / "unbreakable" / "quantum-safe" / unscoped
"end-to-end quantum-resistant" / "CNSA 2.0" / "FIPS 206 / Falcon"; and never imply AES-256
is quantum-broken (it is not). Use "quantum-resistant" / "post-quantum" + the FIPS number +
the hybrid-vs-classical scope. Standard:
[CRYPTOGRAPHY_STANDARD.md](https://github.com/smilinTux/sk-standards/blob/main/standards/CRYPTOGRAPHY_STANDARD.md).

## Secret handling

- **Never commit** secrets, PGP/keystore private keys, `.env` files, or bot tokens. Bot
  tokens live in `~/.config/skchat/*.env` (`EnvironmentFile=` in the systemd unit); PGP
  private keys stay in the agent's capauth key store / `flutter_secure_storage` on-device —
  never logged or transmitted.
- Key material is **injected** into crypto components (the gatekeeper's `signer`/`verifier`,
  the prekey `http_get`) — modules carry no embedded keys.
- Agents sign as **their own** per-agent capauth identity, never the operator key (the
  skcomms agent-signing-key fix). A source-FQID mismatch is a hard rejection.
- CI/pre-commit: run a secret scan; a leaked credential is rotated and scrubbed from history
  before any push (this repo has an established scrub/rotation runbook).

## Dependency posture

- Core: `skcomms` (transport + glossa codec), `capauth` (identity/signing), `skmemory`,
  `pydantic`, `PGPy`, `cryptography` (AES-GCM/HKDF), `cbor2`, `mcp`; optional `click`,
  `rich`, `textual`, `livekit` (lazy-imported — the mesh loads without a live room).
- Hybrid KEM primitives come from `skcomms.pqkem` (X25519 + ML-KEM-768); we do **not**
  hand-roll KEM math in skchat.
- Third-party CVEs are tracked and bumped; report dependency issues upstream. Pin/lock at
  release; the green-bar test gate (`pytest`) blocks a release that regresses crypto tests
  (DM ratchet, group key, at-rest wrap, prekeys, glossa gatekeeper).

## Compliance

Conforms to the sk-standards crypto bar:
[CRYPTOGRAPHY_STANDARD.md](https://github.com/smilinTux/sk-standards/blob/main/standards/CRYPTOGRAPHY_STANDARD.md) ·
[CRYPTO_AGILITY_STANDARD.md](https://github.com/smilinTux/sk-standards/blob/main/standards/CRYPTO_AGILITY_STANDARD.md) ·
[SECURITY_DISCLOSURE_STANDARD.md](https://github.com/smilinTux/sk-standards/blob/main/standards/SECURITY_DISCLOSURE_STANDARD.md).
Maturity tier + per-surface state: **`SOP.md §9`**. Full crypto architecture:
[`docs/crypto-architecture.md`](docs/crypto-architecture.md); master plan
[`docs/quantum-resistance-architecture.md`](docs/quantum-resistance-architecture.md); epic
`PQC-MIGRATION` (coord `e1d6ba2a`).
