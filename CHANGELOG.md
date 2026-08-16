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
- **Browser QA lane (skwatchdog WD-10, card `01b304a5`).** `skchat browser-qa run`
  walks skchat web in a real Chrome over raw CDP, captures a screenshot plus the
  console per step, grades the **image**, and writes one result artifact
  (`~/.skchat/browser-qa/<run_id>/result.json`) that the skos watchdog folds into
  the daily digest as ordinary `WatchdogEvent` records. Report-only: it never
  opens a card, writes a GTD item, or restarts anything.

  Why an image and not the DOM: Flutter web renders into a canvas, so
  `document.body.innerText` is empty on a fully working page. A share-link fix
  shipped with the route correctly present in the compiled bundle and still
  rendered a blank grey screen, because the route did an unguarded cast on a
  router extra that is null for a shared link. It was caught only when a human
  loaded the page. Grading therefore measures pixels: a frame where one colour
  covers 99.5% or more is the blank-screen signature.

  Safety, non-negotiable: the lane **never navigates to an existing Space**
  (`/app/#/spaces/{id}` joins a live call and can publish audio), and
  `assert_safe_url` refuses the whole room route family on every navigation. When
  a run genuinely needs a room it creates its own over plain HTTP and ends it in
  the same run, guaranteed on the failure path by a `try/finally`, a pre-create
  reap record, and a reap sweep at the start of every run. It defaults to CDP port
  **9232**, never 9229 (the daily instance) or 9222/9223 (the agent instances).

  Severity discipline: only deterministic evidence (unreachable API, failed
  navigation, failed capture, blank frame, un-ended Space) earns `problem`, since
  a `problem` files a GTD item. A model verdict of `fail`, a console error, a slow
  boot, a missing browser, and an ungraded run all top out at `notable`. When
  skgateway is unreachable the run carries a noted gap and **no verdict is
  invented**.

  A blind grader is also a fabricated verdict, so the model must read three
  random digits off a generated test image before it is trusted to judge a
  screenshot. Caught in the field during this card: `sk-default` routes to
  `openai/gpt-oss-20b` (`modality: text->text`), which scored a correctly
  rendered onboarding screen 1 out of 5 with "the page shows no usable UI",
  having judged the console log alone. The run now skips with
  `vision_unavailable` instead. Likewise, 401/403 auth challenges from the
  lane's own fresh unauthenticated profile are kept in the evidence but
  excluded from severity, so the digest does not carry a `notable` line every
  morning for behaving correctly. Runbook: `runbooks/browser-qa-lane.md`.

### Fixed
- **The `/api/v1/audience-token` mint endpoint no longer persists a file.**
  `mint_agent_audience_token` is now called with `store=False`: this endpoint
  mints a self-contained audience token per request, so writing a file per mint
  was the same flood substrate the operator-audience path had (card `e793b6bc`).
  The caller still gets the wire form.
- **Operator-audience tokens are no longer persisted (`store=False`).**
  `mint_operator_audience_token` now mints with capauth's `store=False`, so the
  self-contained (signature-verified, never store-looked-up) audience token writes
  no file. This removes the flood substrate at the source; the reuse cache and GC
  become belt-and-suspenders for this token. Follow-up to the cache/GC fix below.
  Card `e793b6bc`.
- **Runaway operator-audience mint (store flood).** `issue_operator_audience` ran on
  every session handshake and minted a fresh capauth audience token each time; each
  mint stores a file, so `capauth/security/tokens` flooded to 38k files / 153MB of
  expired 12h-TTL tokens (none read). Added a per-fingerprint reuse cache (reuse
  until 5 min before the 12h expiry, mirroring the shadow twin cache) so minting
  drops from per-request to about twice a day per device, plus an opportunistic,
  rate-limited GC nudge (`capauth.tokens.prune_expired_tokens`) after a real mint.
  The token and wire form are unchanged; no crypto behavior changes.

### Docs
- **Declared a maturity tier for the first time.** skchat is a crypto component and had
  **no** `T0`-`T4` declaration in README or SOP, while `SECURITY.md` pointed at "`SOP.md`
  §9" for one. Now declared and backed by code: **T1 (Agile) + T2 (Hybrid KEM,
  `HKDF(X25519 ‖ ML-KEM-768)`, FIPS 203) on skchat-owned surfaces (device prekeys, 1:1 DM
  ratchet, new groups, at-rest DEK); T3 (Hybrid sig) NOT claimed, signatures are classical
  Ed25519/RSA; T4 (Transport closed) NOT claimed.** The tier is scoped precisely: skchat
  **consumes** primitives from capauth and skcomms rather than owning them. Documented
  exceptions kept explicit (legacy `rsa-pgp-wrap-v1` groups, the reduced-assurance browser
  leg).
- **Added the mandatory experimental/unaudited posture statement** to README and
  SECURITY.md, per `SECURITY_DISCLOSURE_STANDARD` section 2. Neither file previously
  contained the words audit, experimental, or unaudited. Also added a supported-versions
  table.
- **Corrected a false bind claim in SOP §5.** It declared the webui binds
  `127.0.0.1:8765` and that skchat is "never an internet-exposed port". Verified
  2026-08-15 with `ss -tlnp`: **`0.0.0.0:8765`**, plus a second instance on
  **`0.0.0.0:8766`** (opus). The code default is correct (`webui.py`, `SKCHAT_HOST`
  defaults to `127.0.0.1`); `~/.config/skchat/webui-*.env` sets `SKCHAT_HOST=0.0.0.0`.
  Raw-port exposure is the **LAN `192.168.0.0/16` plus tailnet, not the internet**.
  Separately, Funnel **does** proxy `/` to `localhost:8765`, so `:8765` is internet
  reachable **through the Funnel path** and always was, by design.
- **Corrected the opposite error on `:9385`.** Its bind is right (`127.0.0.1`), but the
  claim that it is "not Funnel-exposed" was wrong: Funnel proxies `/daemon` to it.
- **Declared the undocumented `skchat-app-web` surface** on `0.0.0.0:8088`. This one is
  deliberate (`systemd/units/skchat-app-web.service` overrides the script's `127.0.0.1`
  default because `:8088` is not Funnel-fronted), but an undeclared listener is a
  documentation defect regardless.
- **Stopped referring to a `version` field to bump**; `pyproject.toml` is
  `dynamic = ["version"]` with setuptools-scm deriving from the newest release tag.
- **Narrowed a stale release warning to what is actually dangerous.** SOP §5 said "do NOT
  `git push` skchat, pushing auto-publishes to PyPI". Re-verified 2026-08-15: the local
  `pre-push` hook that cut a tag on any branch push was removed on 2026-08-13 and its
  `.git/hooks/pre-push` symlink is now dangling, and `publish.yml` triggers **only** on
  `push: tags: ["v*"]` plus `workflow_dispatch`, with no branch trigger and no other
  workflow cutting a tag. The real rule is **never push a `v*` tag**. A blanket warning
  that must be violated to do ordinary work is a warning that stops being read.
- Added a `docs-evidence` block (11 hermetic checks, all negative-tested) and the
  `docs-check` CI gate at tiers 1,2.

### Security (crypto-relevant)
- **Voice-call privilege is resolved from the signature-verified invite FQID, not
  from the LiveKit display identity.** New `skchat/voice_engine/caller_profile.py`
  resolves a `CallerProfile` (`operator | companion | guest`) by EXACT match of the
  invite's `from_fqid` (surfaced only by the signature-gated `/call/incoming`)
  against a directory derived from the agent's own sovereign FQID. This retires
  `is_chef_identity()` and `LUMINA_OPERATOR_PREFIXES`, a `startswith("chef")` match
  over caller-supplied display data that was wrong in both directions: it failed
  CLOSED for the operator on 2026-08-13 (his browser joins as
  `lumina@chef.skworld.io`, so the roundtable cap silenced her to him and every
  operator tool was refused mid-call), and it failed OPEN for anyone choosing a
  display name beginning with `chef`. Operator authority now also requires the 1:1
  register and a non-agent speaker, and anything unknown, malformed or absent
  resolves to the least privilege rather than defaulting to operator. Identity only:
  the per-profile tool policy, confirmations, the action ledger and the speakable
  gate are separate changes.
- **`/call/start`, `/call/answer`, and `/call/incoming` are now gated by
  `_gate_token_mint`, matching `/livekit/token`.** Both `/call/start` and
  `/call/answer` mint the same full-publish LiveKit JWT as `/livekit/token`
  (via `_prepare_call` -> `_mint_token`) but previously had no auth check at
  all; `/call/incoming` discloses who is calling whom and was equally
  unguarded. All three now require the same loopback/tailnet origin or a
  valid `SKCHAT_GUEST_OPERATOR_TOKEN` that `/livekit/token` already enforces,
  so a call route is never more open than token minting (see skchat-app
  `SECURITY.md` for the client-facing note).

### Added
- **Group threads carry per-member participants (unified conversation list).**
  `daemon_proxy` `/conversations` group threads now carry per-member
  participants with server-resolved `soul_fingerprint`, feeding the client's
  new aggregate group trust badge (see skchat-app `CHANGELOG.md`). Builds on
  the existing `fingerprint_for_identity` / `member_to_app` resolution below.
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
