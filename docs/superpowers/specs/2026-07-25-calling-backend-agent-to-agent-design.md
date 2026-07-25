# Calling Backend: First Real 1:1 Call (Agent-to-Agent) Design

**Date:** 2026-07-25
**Repos:** skchat (call routes + tests), skcomms (recipient-key sealing), a new answerer service, and `~/.skcapstone` config (peer store re-key).
**Status:** Approved (design). Coord card `da7c941c`, epic `4187787c` (in-thread calling).
**Follows:** Phase 2 in-thread 1:1 calling (client shipped + deployed). Scoped by a 5-agent discovery swarm (run `wf_17cbab96-f96`).

## 1. Goal

Make a real client-initiated 1:1 voice call ring and be answered with two-way
audio, end to end, over the existing `/call/*` LiveKit path. Phase 2 shipped the
client; this closes the server-side delivery + answerer gaps that only surfaced
under live testing.

**First target (this spec): agent-to-agent, Lumina calls Opus.** This proves the
whole transport (resolve, seal, invite, poll, answer, LiveKit audio) with the
smallest surface, and defers the harder "operator identity != agent identity"
problem to a well-scoped follow-on (Option A, section 8).

## 2. Root cause (grounding)

Live testing of Phase 2's caller path hit a 404 then a 500. The swarm traced
**both to a single config drift**, not to four independent gaps:

- `~/.skcapstone/cluster.json` realm was changed to `skworld.io` on 2026-06-30
  (backup `.bak-prerealm-20260630` proves it was `skworld` before). So
  `capauth.resolve_agent_identity("lumina").fqid` now yields
  `lumina@chef.skworld.io`.
- `~/.skcapstone/skcomms/peers.json` (added 2026-06-11, pre-migration) still
  holds the OLD keys `lumina@chef.skworld` / `opus@chef.skworld` (no `.io`).
- Consequences of this one mismatch:
  - `_resolve_peer` (skchat `call_routes.py:109-122`) exact-matches the `peer`
    arg against `peers.json` keys, so a real FQID (or the client's
    `capauth:lumina@skworld.io`) never matches -> **404**.
  - A bare-name probe resolves to the stale `opus@chef.skworld`, passed to
    `skcomms.mailbox.send_message` -> `_load_recipient_key` (`mailbox.py:236-291`).
    Its `same_box` gate (`mailbox.py:269`) compares the fqid suffix
    `chef.skworld` against `operator.realm` = `chef.skworld.io` -> **False**, so
    it skips the local-agent key candidate
    (`~/.skcapstone/agents/opus/capauth/identity/public.asc`, which EXISTS and is
    the right key) and returns None -> `_seal_for_recipient` raises `CryptoError`
    -> **500**.

Both agents are ALREADY paired (present in `peers.json` with TOFU fingerprints)
and both have real local capauth keys. Re-keying the store to the current realm
clears both failures at the source.

**Answerer:** nothing polls `GET /call/incoming` for the dynamic `derive_room`.
`skchat-lumina-call.service` is a Space/companion agent hardwired to room
`lumina-and-chef`, has zero `/call/incoming` references, and 401 crash-loops
(mints `/livekit/token` without the operator token). The skchat-app
`IncomingCallWatcher` is the right shape (polls `/call/incoming`, attaches
`X-Operator-Token` since `520f5fa`), but it runs in the GUI client, not as an
always-on agent endpoint.

## 3. Approved decisions

1. **Realm fix direction:** re-key the peer store UP to `skworld.io` (match the
   live `cluster.json` and the `skworld.io` ecosystem convention). The peer store
   is the stale artifact. Applied consistently fleet-wide (it syncs across nodes).
2. **First topology:** agent-to-agent (Lumina calls Opus). Option A (you call
   Lumina) is a documented follow-on, section 8.
3. **First answerer:** a new minimal always-on auto-answer service running as the
   callee agent (Opus), sending the operator token and polling `/call/incoming`.
4. Security posture unchanged: the operator-token gate stays closed; at-rest
   sealing stays fail-closed; `/call/incoming` anti-spoofing stays.

## 4. Design by layer

### 4.1 Data: re-key the peer store (fixes 404 + 500 at the source)
- Rewrite `~/.skcapstone/skcomms/peers.json` keys and
  `known_fingerprints.json` from `<agent>@chef.skworld` to
  `<agent>@chef.skworld.io`, preserving `syncthing_device_id`, `fingerprint`,
  `added_at`. Grep every consumer of the old string first so nothing orphans.
- Clean migration debris the swarm found: `skcomms/peers/lumina.pub.asc`
  (literal `"fakekey"`), `Lumina.yml` (fabricated fingerprint, case-dup), and
  stale no-pubkey `*.skworld.io` / `lumina-box@` TOFU entries.
- This is a config change under `~/.skcapstone` (not a repo). It is fleet-wide:
  the same re-key must be applied on every node that syncs this store, or a
  partial re-key strands a node exactly as today.
- Acceptance: `_load_recipient_key("opus@chef.skworld.io")` returns a key;
  `_resolve_peer("opus@chef.skworld.io")` resolves.

### 4.2 skcomms: harden `same_box` (defense in depth)
- `mailbox.py:_load_recipient_key` `same_box` comparison (`:269`): compare the
  operator component (or realm-normalize) rather than the exact
  `operator.realm` string, so a future realm rename cannot re-strand a local
  key. The documented threat model is operator-collision, not realm precision.
- Add a debug log distinguishing "same_box rejected" from "no key anywhere."
- Do NOT touch the fail-closed invariant (`_seal_for_recipient` raising on a
  missing key stays), and do NOT widen the operator's-own-key fallback
  (`mailbox.py:285-286`, latent mis-seal risk) to "fix" the 500.

### 4.3 skchat: peer-resolution contract
- Add `capauth:<agent>@<domain>` -> fqid translation in `_resolve_peer` (strip
  the `capauth:` scheme, resolve to the current fqid), so a client holding the
  normal capauth wire URI does not 404.
- Document ONE canonical `peer` format in the `call_routes.py` docstring
  (webui.py and skchat-app currently disagree).
- `LUMINA_ID` in `daemon_proxy.py:42` must be the re-keyed canonical form so the
  client-sent `peerId` matches.

### 4.4 Answerer: minimal auto-answer service
- A new always-on service running as the callee agent (Opus): reads its operator
  token, polls `GET /call/incoming` (sending `X-Operator-Token`), and on a fresh
  signature-verified invite calls `POST /call/answer` (getting
  `{room, token, livekit_url}`), connects to that room with that token, and
  publishes audio. It must join the DYNAMIC `derive_room` room from the invite,
  NOT a hardcoded default. Reuse `lumina-call.py`'s LiveKit connect/publish
  machinery; do not reuse its room logic.
- Systemd unit (env: operator token, webui URL). `disable --now
  skchat-lumina-call.service` to stop the ~20k-restart crash-loop (its 401 is a
  wrong-room dead end, not answerer progress).
- The answerer consumes only already-verified invites from `/call/incoming` (the
  server already anti-spoof-checks); it must not re-parse unverified bodies.

### 4.5 Test seam (the exact place it broke)
- Today `test_call_routes.py` monkeypatches `_list_peers`/`_self_fqid`/
  `_send_invite`, so the real skcomms + capauth integration seam is untested.
  Add an integration test that exercises real `list_peers` + `send_message` +
  `resolve_agent_identity` for a same-box paired agent, asserting `/call/start`
  succeeds through the at-rest seal.

## 5. Data flow (Lumina calls Opus)

Lumina (caller) `POST /call/start {peer: opus@chef.skworld.io}` -> `_resolve_peer`
(now matches) -> `derive_room(lumina_fqid, opus_fqid)` -> `_mint_token` ->
`_send_invite` -> `send_message(opus, CALL_INVITE)` -> `_load_recipient_key(opus)`
(now resolves the local key) -> sealed inbox drop. Opus's auto-answer service polls
`GET /call/incoming` -> sees the verified invite -> `POST /call/answer {peer:
lumina}` -> gets `{room, token, livekit_url}` for the SAME derived room -> connects
+ publishes audio. Two-way audio in `call-<hash>`.

## 6. Security / invariants (must not regress)

- Operator-token gate stays closed (`SKCHAT_GUEST_OPERATOR_TOKEN` +
  `SKCHAT_DATAPLANE_AUTH=1`). The answerer PRESENTS the token; never reintroduce
  a loopback/tailnet bypass to unblock a service.
- At-rest sealing stays fail-closed (`_seal_for_recipient` raises on missing
  key). The `same_box` hardening relaxes realm-string brittleness ONLY.
- `/call/incoming` signature verification + `from_fqid` anti-spoof cross-check
  (`call_routes.py:210-222`) stay.
- The M1b client trust gate (`canCall` blocks red) and aggregate trust badge are
  not bypassed by any new call path.
- The realm re-key is fleet-wide shared state: apply consistently across nodes
  and remove the debris, or a partial re-key strands a node.

## 7. Testing

- skcomms unit: `_load_recipient_key` resolves the local key under a drifted
  realm after the `same_box` hardening; still returns None (and sealing still
  raises) for a genuinely unknown recipient.
- skchat integration (un-monkeypatched): real `list_peers` + `resolve_agent_
  identity` + `send_message` so `/call/start` seals successfully for a same-box
  paired agent; `_resolve_peer("capauth:opus@skworld.io")` resolves.
- Answerer unit: on a fake `/call/incoming` invite it calls `/call/answer` and
  joins the dynamic room (fake LiveKit connect); dedupe by nonce; a poll 401
  surfaces (not silently swallowed forever).
- End-to-end acceptance: Lumina calls Opus, confirm two-way audio in the derived
  room, then `hang up` tears down.

## 8. Deferred: Option A (you call Lumina)

Once agent-to-agent works, "Chef calls Lumina" adds, on top of this: a synthetic
operator caller identity when authenticated via the operator-token path (today
`_self_fqid()` is always `lumina`, so peer=lumina is a self-call); a `chef@<realm>`
pairing entry; a persistent client credential (native `operator_token_io.dart`
caches in-memory only, set solely by the Mode-C screen, so an owner session polls
with a null token and 401s silently); and surfacing 401s in the client
`IncomingCallWatcher`. Same answerer service is reused. Tracked as the Option A
follow-on under `da7c941c` / a new card.

## 9. Delivery

Ordered tasks (TDD where code, verified where config):
1. [config] Re-key `peers.json` + `known_fingerprints.json` to `skworld.io`
   (grep consumers first). Verify `_load_recipient_key` resolves.
2. [config] Remove migration debris (fakekey, fabricated fingerprint, stale
   no-pubkey TOFU entries).
3. [skcomms] Harden `_load_recipient_key` `same_box` (operator-component /
   realm-normalize) + debug log. Unit test.
4. [skchat] Un-monkeypatched integration test of the real seal seam.
5. [skchat] `_resolve_peer` accepts `capauth:` URIs + canonical-format docstring;
   fix `LUMINA_ID`.
6. [answerer] Minimal auto-answer service (poll -> answer -> join dynamic room ->
   publish audio), running as Opus, sending the operator token. Unit test.
7. [config] Systemd unit for the answerer; `disable --now skchat-lumina-call`.
8. [e2e] Lumina calls Opus, confirm two-way audio (acceptance).

Realm re-key must be applied fleet-wide (all syncing nodes) as part of task 1.
