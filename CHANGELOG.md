# Changelog

All notable changes to **skchat** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

skchat is a **crypto component** (see `docs/crypto-architecture.md`); crypto-relevant
changes are called out explicitly so claims stay evidence-backed per the
[sk-standards](https://github.com/smilinTux/sk-standards) doc/SOP + cryptography
standards.

## [Unreleased]

### Added
- **Per-member + per-participant capauth fingerprint (M1b trust badges).**
  `daemon_proxy.fingerprint_for_identity()` resolves a member/participant identity
  to its real capauth fingerprint from the peer store (Lumina special-cased).
  `member_to_app` emits it under both `soul_fingerprint` and the `fingerprint`
  alias for `GET /groups/{id}/members`; conf/Space/call participant tokens embed
  it in LiveKit participant metadata (`spaces/tokens.py` `_build_token` gains a
  `metadata` param) so the client can anchor a per-participant trust badge.
- **resilience-v1 folded into `main`** (tag `v0.14.105`): the operator auth gate
  (`dataplane_auth`, `operator_auth*`), Spaces work, and the resilience-v1
  hardening are now on the canonical branch, matching what is deployed.

### Security (crypto-relevant)
- **Trust-badge fingerprint is stamped ONLY from a cryptographically-proven
  identity.** An earlier draft stamped it on the unauthenticated
  `/conf/{room}/token` and `/spaces/{id}/join` join routes, where a caller could
  claim any keyed agent's identity and wear its badge (a trust spoof, caught by
  adversarial review). Now stamped only after `verify_signed` (`/join/sovereign`,
  Space/conf federation authd, conf `federated-token`); the unauthenticated and
  operator-gated-but-caller-chosen (`/livekit/token`) paths stamp nothing.
  Regression tests assert the public routes do NOT stamp.
- **`fingerprint_for_identity` is STRICT** (full identity/handle/fqid only, no
  bare short-name), closing a cross-realm collision where a remote
  `artisan@opB.skworld.io` would inherit the LOCAL `artisan`'s key.
- **Space moderation round-trips `soul_fingerprint`** (`StageState`), so a
  hand-raise / invite no longer clobbers a participant's trust-badge fingerprint.

## [0.14.0] - 2026-07-03

### Added

- **SKGlossa G2 — runtime rate adaptation** (2026-07-03). New
  `skchat.glossa_mesh.rate.RateController`: an adaptive tier selector with
  asymmetric hysteresis (degrade fast — one tier down per bad observation toward
  the robust L0 floor; upgrade slowly — a sustained good streak before stepping
  back up toward the ceiling), so a link can rate-adapt within the hard tier
  ceiling that handshake negotiation fixes. Pluggable quality signal via
  `observe(score)` or `observe_network(loss, latency_ms)`; `quality_from_network`
  maps loss/latency to a `[0,1]` score. The controller only ever *proposes* a
  tier — `level(ceiling)` clamps it into `[floor, ceiling]`, so it can never
  exceed what negotiation allows. Wired into `glossa_mesh.node` / `.session`.
- **SKGlossa L3 — token-stream codec** (2026-07-03). New
  `skchat.glossa_mesh.tokenstream` adds tier **L3**, a strictly-additive tier
  *above* the skcomms L0/L1/L2 ladder. Where L0-L2 encode a Message as one
  self-contained frame, L3 emits the Message as an ordered CBOR stream of small
  typed tokens (`INTENT · ARG* · REF* · TEXT* · END`) so a receiver can begin
  glossing before the whole frame arrives (streaming) and split the text slot
  across chunks. Round-trip invariant `decode_l3(encode_l3(m)) == m`. Re-exported
  via `glossa_mesh.codec_ext` alongside the L0-L2 constants; skcomms itself stays
  unmodified. Gated behind tier negotiation — a peer without L3 stays on the
  prior tier (never an undecodable frame).
- **Engine-backed LiveKit transport** (2026-07-03). New
  `skchat.transports.livekit` re-homes the out-of-tree lumina-call agent
  (Phase-3) onto the unified `skchat.voice_engine` brain. The VoiceEngine owns
  the brain (persona · memory · forced-routing · LLM · tools · STT/TTS); the
  transport owns the room/turn loop (per-participant energy VAD, barge-in, the
  addressing gate, and the multi-agent roundtable turn-cap), pushing PCM into a
  LiveKit `LocalAudioTrack`. The decision logic (`VADSegmenter`,
  `BargeInDetector`, `AddressingGate`) is factored into pure injectable-clock
  classes, unit-tested without a live room. `livekit` is a **soft dependency** —
  importing the module never requires the RTC SDK (only `run_agent` /
  `build_room_session` do), matching the `livekit_routes.py` policy.
- **SKCHAT_HOME-aware per-agent history store** (2026-07-03). `ChatHistory`'s
  JSONL + memory-store paths now resolve from `SKCHAT_HOME` (via `_skchat_home()`),
  defaulting to `~/.skchat` when unset. Two agent daemons/webuis on one box no
  longer co-mingle one message store — e.g. an `opus` daemon + webui@opus run with
  `SKCHAT_HOME=~/.skchat-opus` and keep opus's inbox fully separate from lumina's.
  Behaviour is unchanged for existing single-agent setups. `SKCHAT_ADVOCACY_DISABLED`
  also lets an external responder own replies without the built-in AdvocacyEngine
  double-answering.

### Notes

- **Crypto surface unchanged.** G2 (rate adaptation + L3 token-stream) is a
  codec/rate concern, not a crypto change: it alters framing/tier selection, not
  key exchange, signing, or cipher choice. No entry in `docs/crypto-architecture.md`
  is required for this release. The G2 tier remains gated behind the same
  handshake negotiation, so it introduces no new undecryptable-frame path. The
  per-agent store is a filesystem-isolation concern, likewise crypto-neutral.
