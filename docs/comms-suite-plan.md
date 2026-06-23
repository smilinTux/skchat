Everything is confirmed against the real code. The exact service ids (`text`, `voice`, `video`, `file-transfer`, `data-streaming`, `federation`, `access-plane`, `geo-cot`), transport ids, plugin entry-point group (`skchat.plugins`), and the hard-coded shell tabs / route constants all match the inventory. I have what I need to write a precise, buildable plan.

---

# skchat Sovereign Comms Suite — Architecture & Phased Roadmap
**A Nextcloud-Talk-class, plugin-able comms platform built into skchat**
Lead architect doc · grounded in live code · 2026-06-23

---

## 0. Executive summary

We already have ~80% of a Nextcloud-Talk-class suite shipped and live: 1:1 text + 1:1 LiveKit video, SK Spaces (on-the-fly audio rooms with roles/raise-hand/moderation), screenshare, opt-in recording→minutes, a CoT map UI, three plugin surfaces, capauth-signed federation, and an honest `/api/v1/capabilities` discovery doc. What is missing is **cohesion and modularity**: the app's surfaces are hard-coded (`app_shell._tabs` const, ~30 hand-written `GoRoute`s, a duplicated `HubScreen` tile list), the message wire is text-only (`ContentType` = PLAIN/MARKDOWN/SYSTEM), Spaces are audio-only with no multi-camera grid, the map feed is a stub, docs are last-write-wins, and there is no uniform "agent as a room participant" contract.

This plan does three things:
1. **Unifies the model** — adopt Nextcloud Talk's *single-token room* (chat + call are one entity) and *Rich Object* typed-message envelope so every feature (mentions, files, polls, location, agent cards) rides one schema.
2. **Makes it modular** — a `ModuleManifest` registry on the Flutter client + a `modules` block on the backend capabilities doc, with contribution slots (nav tab / toolbar icon / swipe-up drawer / screen / settings), capability-gated availability vs settings-driven placement.
3. **Ships in phases** — hardened 1:1 → groups → on-the-fly group video+screenshare → location → agent-collaborative docs → recording→minutes, each a self-contained module with acceptance criteria.

**Key repos/services in play:** `skchat` (Python daemon, `src/skchat/`), `skchat-app` (Flutter, `lib/`), `skcomms` (transport router + `capabilities.py`), `capauth` (identity), LiveKit SFU `:7880` + coturn, faster-whisper `.100:18794`, qwen3.6 `.100:8082`, Nostr relay `:7447`, sk-lk-authd federation.

---

## 1. Target experience (Nextcloud-Talk-modeled)

The north star is **one room is one entity** across its whole lifetime:

| Stage | Talk model | skchat target |
|---|---|---|
| 1:1 chat | conversation `roomType=1` | A `Room` keyed by FQID-pair (already: `call_session.derive_room`) holding chat history + ACL |
| → group | `roomType=2` | Same Room primitive, N members; promote a 1:1 by adding members (no new object) |
| → on-the-fly voice/video | `POST /call/{token}` flag flip | `in_call` state on the Room; LiveKit room name **==** Room token. Mint token, peers join |
| → screenshare | second publisher track `roomType:'screen'` | `screen_share_panel.dart` track on same participant (already works) |
| location pin / live | Rich Object `geo` param | Typed `LOCATION` / `LOCATION_LIVE` message → renders into `skmap` |
| agent doc/plan drafting in-room | bot + canvas | Lumina as a CRDT awareness peer in a `doc` lane with live cursor |
| recording → minutes by default | recording service + call_summary_bot | egress→Whisper→qwen map-reduce, auto-posted minutes message |

Concretely, the user journey we are building toward:

> Chef opens a 1:1 with Lumina → types, gets reactions/threads/edits → taps "call" and it becomes a 2-person LiveKit room *in the same thread* → invites Jaime, it's now a 3-person video grid → someone drops a live location pin that animates on the in-room map → Lumina, present as a real participant, drafts a meeting plan live in a shared doc with her own cursor → when the call ends, diarized minutes (decisions + action items with owners) appear automatically as a message in that same room, and the action items land in GTD/coord.

---

## 2. Modular plugin architecture

This is the structural spine. The pattern (validated across VS Code, Obsidian, Nextcloud, Matrix widgets, Flutter) is **declarative manifest + lazy activation + named contribution points**, with the decisive split: **availability** (capability-gated, global) vs **placement/visibility** (settings-driven, per-slot).

### 2.1 The two tiers (be explicit)
- **Tier A — first-party modules**: compiled-in Dart + Python. Trusted, no isolation (Flutter `deferred as` defers *loading*, not isolation). This is everything we ship: skmap, spaces, docs, polls, location, recordings.
- **Tier B — external widgets** (future, reserved): untrusted sub-apps mount via iframe (web) / WebView (native) + a `postMessage` capability-negotiation bridge modeled on `matrix-widget-api`, scoped by capauth. Do **not** build now — but make the manifest's `requires`/`role` fields forward-compatible so the approval flow drops in later.

### 2.2 Flutter client: the module registry

New files under `skchat-app/lib/core/modules/`:

**`module_manifest.dart`** — const-constructible model:
```
class ModuleManifest {
  final String id;                 // 'skmap', 'location', 'polls'
  final String title;
  final IconData icon;
  final String route;              // GoRoute path, e.g. '/skmap'
  final int version;
  final int minDaemonApi;          // gates against capabilities.api
  final List<CapabilityRef> requires;  // ['service:geo-cot','transport:webrtc']
  final ModulePlacement defaultPlacement; // nav | toolbar | drawer
  final ModuleRole role;           // everyone | operator
  final int order;
  final Future<void> Function(ModuleContext)? onActivate; // Obsidian register* model
}
```

**`module_registry.dart`** — `const List<ModuleManifest> _builtinModules` (migrate the current 30 routes + 5 tabs + Hub tiles into manifests). Providers (matching the app's Riverpod-everywhere style):
- `moduleRegistryProvider` — all manifests
- `enabledModulesProvider` — registry ∩ `nodeCapabilitiesProvider` (a module lights up only when **every** `requires` ref resolves to CapStatus up/configured/degraded; down/unconfigured → rendered **greyed with a reason**, never hidden — that is the existing app honesty principle)
- `navModulesProvider` / `toolbarModulesProvider` / `drawerModulesProvider` — placement-filtered, watched by `AppShell`

**`module_prefs.dart`** under `lib/services/` — Hive-backed (already a dep, same pattern as `theme_provider.dart` / `daemon_config.dart`): `{enabledIds:Set, placement:Map<id,slot>, order:Map<id,int>}`. **Final enabled set = settings ∩ capabilities.**

### 2.3 Contribution slots (VS Code `contributes` analog)
| Slot | Where it renders | The user-facing payoff |
|---|---|---|
| `nav` | bottom-nav tab | core sub-apps (Chats, Activity, Calls) |
| `toolbar` | per-screen AppBar action | **"promote maps onto the main toolbar"** |
| `drawer` | swipe-up app drawer | **the "all enabled sub-apps" list** |
| `screen` | routable GoRoute | full-screen module |
| `settingsTab` | under Me/profile | module config |

### 2.4 The two surfaces the user explicitly asked for
- **Swipe-up app drawer**: a Flutter `DraggableScrollableSheet` (the 2026 bottom-sheet standard, Telegram/Apple-Maps/One-UI-8.5 gesture precedent) fed by `drawerModulesProvider`, a grid of every enabled module grouped by role. Coordinate z-order with the existing `PiPOverlay` / incoming-call push in `AppShell` (the shell already juggles call overlays + offline banners — do not stack blindly).
- **Promote-to-toolbar**: the placement chooser in the Modules settings tab moves a module's slot from `drawer`→`toolbar`; `toolbarModulesProvider` then renders its icon in the AppBar. SkMap is the pilot.

### 2.5 Kill the drift (the concrete refactor)
Today adding a feature means editing **three files in lockstep**: `app_shell._tabs` (const), `app_router.dart` (~30 GoRoutes), `hub_screen.dart` (`_OpsTile` list). After this:
- `AppShell._tabs` const → `ref.watch(navModulesProvider)`; `_indexFor`'s hard-coded `location.startsWith()` chain → derived from manifests.
- `app_router.dart` shell children → generated by mapping manifests (route + **deferred** builder so disabled modules don't tax cold start). Keep top-level non-shell routes (`/call/*`, `/join`, viewer) hand-written — they're not user-toggleable.
- `HubScreen` tiles → generated from `drawerModulesProvider` (or retired in favor of the drawer).

### 2.6 Backend: skcomms module discovery
Extend `skcomms/src/skcomms/capabilities.py build_capabilities()` to **additively** emit:
- a top-level `api` integer field (so `minDaemonApi` can gate), and
- a `modules` block: per-node list of module ids the **node** wants surfaced (operator policy — which sub-apps this deployment ships).

The contract stays additive and null-tolerant (the Flutter `capabilities_service.dart` parser already null-tolerates missing keys; never restructure the existing `transports`/`services` arrays). A manifest's `requires` references the **existing** service ids verbatim — `service:text`, `service:voice`, `service:video`, `service:file-transfer`, `service:data-streaming`, `service:federation`, `service:access-plane`, `service:geo-cot` — and transport ids `transport:webrtc`, `transport:nostr`, etc. **No new backend contract is needed for the first cut.**

### 2.7 Backend: typed-message contract (the other half of modularity)
The single biggest backend gap is that `ContentType` is text-only. Introduce **one generic typed-module-message contract** (Talk's Rich Object String pattern) so every module is plug-and-play instead of a bespoke code path:
- `content_type = "application/skchat.<module>+json"` (e.g. `application/skchat.location+json`, `application/skchat.poll+json`)
- structured payload in the **existing** `ChatMessage.metadata` dict (so the skcomms envelope + E2E text encryption are unchanged)
- **mandatory** human-readable `body` text/markdown fallback (Matrix MSC1767 extensible-events principle — dumb clients degrade, never break)
- optional `SYSTEM`-message side-effects
- each module's `skchat.plugins` plugin owns `render(on_inbound)` / `compose(on_command)` / `aggregate(server)`

Every module from Phase 4 onward (location, polls, events, agent action-cards) is an instance of this one shape.

---

## 3. What to borrow from Nextcloud Talk (adapted to LiveKit + skcomms + capauth)

| Talk primitive | Borrow | Adapt to our stack |
|---|---|---|
| **Single-token room** (call = state on conversation) | The model itself | skcomms Room FQID == LiveKit room name == chat thread id. `call_session.derive_room` already SHA256→base32 derives a deterministic room; generalize from per-pair to per-Room. Mirror `POST/PUT/DELETE call/{token}` as state ops. |
| **In-call flags bitmask** (IN_CALL=1, AUDIO=2, VIDEO=4, SCREEN=16) | Verbatim | Map LiveKit per-participant track publication → this bitmask in Room presence, so "who's on with what media" is one cheap GET (mirrors `GET /call/{token}`). |
| **Talk Bot API v1** (ActivityStreams 2.0 inbound Create/Like/Join; `POST /bot/{token}/message`; HMAC-SHA256(random+body, per-bot-secret) both directions; `silent` flag; CLI-only install) | The contract verbatim | Define **skcomms Bot API v1**. Lumina = first bot. capauth/PGP for human identity + provisioning; the lightweight nonce+HMAC for high-frequency machine callbacks (agent posts, recording store, presence) — PGP per-message is overkill. Dual mode: webhook (external) OR in-process skcomms lane handler. Constant-time hex compare; never web-exposed self-serve install. |
| **Recording = real participant** (headless join, `recordingConsent` gate, HMAC store callback) | The join-as-participant pattern | We already have `scripts/lumina-livekit-agent.py` joining as a real participant + `recording.py` RoomCompositeEgress. Prefer **LiveKit server-side egress** over a literal headless browser (Talk's fragile/heavy piece). Keep `consent.py ConsentLedger` as the `recordingConsent` analog. |
| **Rich Object envelope** (text + typed parameters) | The schema | §2.7 typed-message contract. |
| **Breakout = child-of-parent room** (lobby + cascade) | The reuse-the-primitive idea | SK Spaces sub-rooms = child Rooms (lobby gate + cascade-delete), reusing the Room primitive. Matches the existing Spaces concept. |
| **Compact ACL enums** (lobbyState, listable, readOnly, hasPassword, messageExpiration, recordingConsent, mentionPermissions) | The serializable enum ACL | Map onto capauth scopes → gives the sovereign-conf-calls guest/lobby/public-funnel gates a federation-friendly ACL. |
| **`lastPing` TTL reaping** | The discipline | Implement ping/TTL reaping or rooms show phantom participants (Talk's known failure mode). `presence.py` is the home. |
| **HPB signaling pattern** (clients SFU-agnostic, server relays offer/answer/requestoffer) | The principle, not Janus | LiveKit already hides the SFU. Keep clients SFU-agnostic. Do **not** rebuild NATS+etcd+Janus topology — lean on LiveKit's Redis-mesh clustering. |
| **Federation = owner-centric proxy** (host-owned room, remote = federated attendees, shared-secret trust) | The model | Matches our skfed envelope philosophy + the proven `spaces/federation/authd.py` (sk-lk-authd capauth-signed FQID → SFU token). Do NOT full-replicate rooms. |

---

## 4. Mapping to our stack (reuse X / extend Y / build Z)

| Capability | Reuse | Extend | Build |
|---|---|---|---|
| 1:1 chat | `models.py`, `history.py`, `group.py`, `reactions.py`, `presence.py`, `outbox.py`; 40 MCP tools | typed `ContentType` (§2.7); edit/edit-history; pin | swipe-to-reply + reaction-tray Flutter row widget; read receipts |
| Groups | `group.py` (encrypted), `create_group`/`group_send` MCP | Room ACL enums; `@all` mention gate; announcement (one-way) child room | Space/Community hierarchy UI; unread filter |
| 1:1↔group video | `livekit_routes._mint_token` (already mints `can_publish` video), `connectivity.ice_config` tier ladder, `VideoTrackRenderer`, `screen_share_panel.dart` | Room == LiveKit room; in-call flags bitmask | **multi-camera video tile grid** (UI/role gap — infra exists); persistent minimized call dock / PiP |
| Screenshare | `screen_share_panel.dart`, `setScreenShareEnabled()` | server-side policy (who can share) | TrackComposite egress of the share track for recording |
| Location | `skmap/` (flutter_map+OSM, CoT typing), `geo_units_source.dart` adapter seam, CoT/TAK bridge on .158, `geo-cot` capability probe | typed `LOCATION`/`LOCATION_LIVE` messages; dual-publish pulses as CoT PLI | **`GET /api/v1/geo/units` + GeoStore route + SSE/WS stream**; publish-my-location endpoint; live-beacon TTL (closes the single TODO at `geo_units_source.dart:165`) |
| Agent-collab docs | `spaces/lanes.py` LaneDispatcher+LaneStore (SQLite replay) as the CRDT substrate; `doc_panel.dart` | swap last-write-wins for Yjs binary updates over the same `doc` lane | yrs (Rust) + `flutter_rust_bridge` CRDT core (AppFlowy pattern); Lumina as awareness peer; doc model/versioning/export in skmem-pg |
| Recording→minutes | `recording.py` (egress), `recording_writeup.py` (Transcriber/Summarizer/Poster seams), `consent.py`, faster-whisper .100:18794, qwen3.6 .100:8082, `transcribe_audio_file` MCP, `capture_to_memory` | per-track WS egress (pcm_s16le) for diarization-free speaker labels; map-reduce JSON-schema reduce | LiveKit **Egress service** as a dedicated CPU worker (NOT on .100/5060); live-caption surface; action-items → `coord_create`/`gtd_capture` |
| Module system | `plugins.py` (ChatPlugin ABC, `skchat.plugins` entry points), `capabilities.py`, Spaces lanes | `modules` block + `api` field in capabilities | Flutter `core/modules/` registry + providers + Modules settings tab + swipe-up drawer |
| Federation / auth | `spaces/federation/authd.py` (capauth-signed FQID→SFU token), Nostr relay :7447 | harden local `/spaces/create` create/join with the same capauth verify (S5) | E2EE key distribution gated per verified identity |

---

## 5. Phased roadmap

Each phase is a **shippable module/plugin**. Branch convention: `feat/<module>` in `skchat` + `skchat-app`. Spec-first under `docs/superpowers/specs/` (repo practice), TDD via the superpowers skills.

> **Phase 0 (prerequisite, lands with Phase 1): the module spine.** Build `core/modules/` registry + providers + `module_prefs` + Modules settings tab; add `modules` block + `api` field to `capabilities.py`; pilot by migrating **skmap** into a manifest (`requires:['service:geo-cot']`, defaultPlacement:`drawer`), delete its hand-coded Hub tile, prove the toggle + promote-to-toolbar + capability-gating loop. **AC:** SkMap toggles on/off in settings, can be promoted from drawer→toolbar, greys out when `geo-cot` is down. **Risk:** low — but this is load-bearing; don't generalize until the skmap pilot is green end-to-end.

### Phase 1 — Hardened 1:1 chat (module: `chat-core`)
- **Build:** typed-message contract (§2.7) — the foundation everything else rides; the message-row interaction kit (swipe-to-reply, long-press reaction tray with 6 suggested + overflow, double-tap-to-edit with 24h/edit-history badge, typing indicator, read/delivery receipts, pinned messages); per-conversation scoped sub-views (Media/Files/Links/Threads/Pinned via `search_messages`/`list_transfers`/`list_threads`); Threads 2.0 (subscribable, drafts, permalink). Single-token Room model (call = state, not a new object).
- **Reuse:** `add_reaction`/`get_reactions`/`send_typing_indicator`/`get_thread` MCP; `history.py`; `outbox.py`.
- **Components:** `skchat/models.py` (ContentType extension), `skchat/history.py`, `skchat-app/lib/features/conversation/`.
- **AC:** swipe-to-reply + reactions + edit + receipts work; a typed message with an unknown content_type renders its `body` fallback; chat and call share one token/history.
- **Risk:** low. Watch: edit-history storage migration in SQLite.

### Phase 2 — Groups & Community hierarchy (module: `groups`)
- **Build:** Room ACL enums (lobbyState, listable, readOnly, hasPassword, messageExpiration, recordingConsent, mentionPermissions) mapped to capauth scopes; Space/Community > rooms > threads hierarchy with a one-way Announcement child room (auto-joined members); `lastPing` TTL reaping.
- **Reuse:** `group.py` (encrypted), `create_group`/`group_add_member` MCP, `presence.py`.
- **AC:** create group, promote 1:1→group by adding members (same Room), set message-expiration TTL, announcement channel broadcasts read-only; no phantom participants after a hard disconnect.
- **Risk:** medium — ACL-enum ↔ capauth-scope mapping must be exact or you leak access. Reaping logic is a classic source of off-by-one presence bugs.

### Phase 3 — On-the-fly group video + screenshare (module: `calls`)
- **Build:** multi-camera video tile grid as a first-class surface (Spaces are audio-only today); Call Links as a first-class capauth-signed object (Signal model) with join policy `{open|approve}`, raise-hand queue, lobby/waiting room, remove+block, aggregated floating reactions; persistent minimized call dock; in-call flags bitmask in presence.
- **Reuse:** `livekit_routes._mint_token` (video grants already there), `spaces/` REST + roles + moderation, `connectivity.ice_config`, `VideoTrackRenderer`, `screen_share_panel.dart`, Sovereign Conf Calls epic `3ceb6da1`.
- **Config (must-do):** `adaptiveStream:true` + `dynacast:true` in Flutter `RoomOptions`, bind tile lifecycle (pinned/thumbnail/offscreen) → layer hints (the 92%-bandwidth lever on residential uplink); VP8/H.264 simulcast default, VP9/AV1 SVC for agent/Chromium rooms; embedded LiveKit TURN/TLS on 443 (drop standalone coturn dependency where possible — document the 443-advertisement gotcha in `runbooks/` next to the sksso-cloudflared notes).
- **AC:** 3+ camera grid escalates from a 1:1 with no renegotiation (everyone-in-a-room model — do NOT hand-roll P2P→SFU); screenshare shows as a track; call link with approve-queue + block works before any public exposure; a 6-person room stays under bandwidth budget with adaptiveStream on.
- **Risk:** **high.** (1) `/spaces/create` currently trusts the tailnet and mints roomAdmin to whoever asks — **S5 capauth verification of the operator assertion is required before any public/guest exposure** (the federation `/sfu/get` path is already verified; reuse `authd.py`). (2) Don't ship P2P mesh for groups. (3) TURN/TLS cert footguns (self-signed doesn't work).

### Phase 4 — Location module (module: `location`)
- **Build:** typed `LOCATION` (static pin, RFC5870 `geo:` URI + asset type self/pin) and `LOCATION_LIVE` (Telegram wire model: `{geo, accuracy_radius_m, heading_deg, period_s, proximity_radius_m, stopped}`) messages; live location as **edit-of-one-beacon-message** (never a stream — keeps timeline clean, expiry natural, stop explicit); `GET /api/v1/geo/units` + server-side GeoStore + SSE/WS stream; publish-my-location endpoint; adaptive cadence (~2-5s moving, 30-60s stationary, hard expiry); proximity alerts (server-side distance check → SYSTEM message); dual-publish live pulses as **CoT PLI** events (period→CoT stale) so chat participants appear as ATAK icons.
- **Reuse:** `skmap/` UI, `geo_units_source.dart` adapter (closes the TODO at `:165`), CoT/TAK bridge on .158, `geo-cot` capability, `/spaces/lanes` (or typed messages over `text`), `security_audit_log` MCP for the share audit trail.
- **Privacy defaults (engineering-enforced, GDPR-2026):** opt-in only, **approximate precision by default** (precise = explicit per-share toggle), bounded durations (15m/1h/8h; "until I stop" = deliberate extra tap), always-visible "sharing with N people — Stop" banner, one-tap revoke, audit entry per start/stop. Module greys out when `geo-cot` is down.
- **AC:** drop a pin → renders on in-room map + as `body` fallback elsewhere; live share animates one beacon, expires on schedule, stops explicitly; coarse-by-default; proximity alert fires; appears in ATAK via CoT.
- **Risk:** medium — privacy correctness is the gate; never let one user edit another's beacon (the early-MSC3489 flaw — use owned/per-sender state).

### Phase 5 — Agent-collaborative docs (module: `docs`)
- **Build:** Yjs CRDT (replace `doc_panel.dart` last-write-wins) — **yrs (Rust)** core shared by server + Flutter via `flutter_rust_bridge` (AppFlowy pattern; there is no mature Dart-native rich-text CRDT). Lumina as a **server-side CRDT awareness peer** (name/color/live cursor/status thinking·composing·idle) — *not* a client bolt-on. Streaming-markdown→CRDT-op parser (agent output = real bold/heading/list nodes, not escaped strings). Relative-position anchors (yrs `RelativePosition`) for ALL agent edits. **Suggestion/diff mode as DEFAULT** (tracked-changes overlay + Approve/Reject; arxiv finding: 23% feel loss-of-control with autonomous edits) — live co-write is an explicit per-room opt-in. Agent edit loop as MCP tools mirroring Electric's set: `doc_snapshot`, `search_text`→relpos handle, `place_cursor`, `insert/replace`, `start_streaming_edit{mode}`. Doc model/versioning/export persisted in skmem-pg.
- **Reuse:** `spaces/lanes.py` (drop Yjs binary updates onto the existing `doc` lane + replay/catch-up), capauth per-agent signing (skcomms agent-signing-key fix → audit which edits were Lumina's), qwen3.6 .100:8082 for bulk drafting + Opus via `model_route`/SKGateway for high-stakes rewrites, skmem-pg `docs` table.
- **Build order:** (1) prototype on web with Tiptap+Hocuspocus+qwen to validate UX cheaply; (2) port CRDT core to yrs+`flutter_rust_bridge`; (3) add suggestion-mode + capauth-signed ops + skmem-pg snapshots.
- **AC:** two humans + Lumina co-edit with live cursors; concurrent edits merge conflict-free; agent edits land as approvable suggestions by default; agent output is structured rich text; doc survives the Space (named, versioned, exportable) instead of evaporating with the lane.
- **Risk:** **high / most engineering.** The FFI core is real work (no turnkey Flutter CRDT). Pitfalls: treating the doc as a string (must stream tokens→CRDT ops), integer offsets (corrupt under concurrency — use relative positions), uncompacted token-per-op (1000-4000 WPM agent floods the wire — batch at agent layer + server compaction).

### Phase 6 — Recording → minutes (module: `minutes`)
- **Build:** **LiveKit Track Egress over WebSocket** (audio-only, pcm_s16le 48kHz, per participant) → faster-whisper → per-track transcripts interleaved by timestamp into one **speaker-labelled** transcript (speaker = LiveKit participant identity — **diarization-free**; pyannote only as a fallback for known multi-voice tracks); qwen3.6 **map-reduce** with strict JSON schema `{title, attendees[], decisions[], action_items[{owner,task,due_date,priority,status}], open_questions[], next_steps[]}` (4-layer structured-output prompt + self-validate + capped list sizes); **minutes-by-default UX** (AutoEgress on `record=true` rooms → post-call hook runs the reduce → posts minutes message into the same Room + sk-alert host — no "generate summary" button); store minutes in skmem-pg `docs` (mxbai-embedded → searchable via `hybrid_search_docs`); action_items → `coord_create`/`gtd_capture`; optional Granola-style live-note augmentation (Lumina/host notes merged into reduce prompt).
- **Reuse:** `recording.py` (egress), `recording_writeup.py` seams (accept a live transcriber + richer summarizer without rewriting the orchestrator), `consent.py`, `transcribe_audio_file` + `capture_to_memory` MCP, faster-whisper .100:18794, qwen3.6 .100:8082.
- **Deploy (must-do):** **Egress service on a dedicated CPU worker, NOT on .100/5060** (egress is CPU+Chrome heavy; .100 VRAM is near-limit, `--parallel 1` serialized). Egress MUST share the **same Redis** as the LiveKit server (silent failure otherwise). **Sequential VRAM**: transcribe→free→summarize; never co-load pyannote+whisper+qwen; keep whisper on 5060 Ti or CPU (Arc iGPU corrupts generation). Consent gate: stay-or-leave disclaimer on egress start, consent record in Room metadata, default-deny recording in rooms with external/guest participants unless accepted.
- **AC:** end a recorded call → diarized minutes with correctly-attributed action items appear automatically in the Room within minutes; action items become coord/GTD tasks; minutes are searchable in skmem-pg; consent banner shown + logged.
- **Risk:** medium-high. Track egress can record the WRONG track if a subscription manager calls UpdateSubscriptions on the egress participant — use a dedicated egress identity / pinned subscriptions. mxbai ctx=512 truncates ~1100 chars → chunk minutes before embedding. Don't treat raw A/V as the durable artifact (expensive, high-retention-risk) — minutes+transcript are durable, media short-retention.

---

## 6. 2027 best-of-breed UX

- **IA — 5-tab bottom nav:** `[Chats | Activity | Calls | Modules | You]`. Chats = unified 1:1/group list, swipe-to-reply rows. Activity = consolidated mentions + thread-replies + reactions + **MCP/agent notifications** + a "Later" save-for-later filter (high value given agent-to-agent traffic). Calls = Signal-style call-link list (create/share/join, approve queue). Modules = the swipe-up drawer. You = presence/identity/soul/theme. **Provide a fast cross-context swipe escape hatch** (Discord's 2026 tab-split drew heavy criticism; keep a You-Bar-style swipe between DMs and current context).
- **Toolbar/drawer:** bottom-sheet (`DraggableScrollableSheet`) is the universal secondary-content container (compose options, module drawer, settings, share, call controls). Per-user pin-to-toolbar + "More" overflow (Slack model). **Default-minimal + opt-in** (Chats/Calls/Files/Voice enabled; worship/render/GTD/memory opt-in per agent identity) — over-modularization makes the drawer a junk drawer.
- **Call UX:** grid + speaker view, raise-hand ordered queue, ephemeral floating reactions, lobby/waiting room with approve/decline, remove+block, persistent **minimized call dock** so calls don't block navigation, live-notes canvas attached to the call → captures to skmemory via `capture_to_memory`.
- **Gestures:** taught in-context (first-use haptic/animation, no tutorial walls); **all primary actions in the bottom third** (thumb zone) — the user's swipe-up-from-bottom instinct is correct; honor it over a top toolbar.
- **Adaptive-but-explicit:** auto-surface frequent peers/modules by recent usage, but **never auto-hide a module the user pinned** (auto-removing configured items erodes trust).
- **Privacy defaults:** location opt-in, approximate-by-default, bounded duration, always-visible active-share banner, revoke-as-easy-as-grant; recording consent banner + default-deny with guests.
- **Honesty:** capability-down modules render greyed with a reason, never vanish (existing app philosophy).

---

## 7. Open questions / decisions for Chef

1. **E2EE vs agents/recording — the central tradeoff.** Media E2EE is the biggest security gap (SFU sees all media today; only DTLS-SRTP + sovereign relay). LiveKit Insertable-Streams E2EE (per-room or MLS key provider, ~0 server overhead) is the 2026 standard — **but E2EE media is unreadable by the recording egress AND by Lumina-as-participant.** Decision: ship a **hybrid** — E2EE for human-only rooms, auto-opt-out (with explicit consent UI) the moment an agent/recorder is present? Or default to no-E2EE for the sovereign-tailnet case and reserve E2EE only for public-funnel/guest rooms? This gates Phases 3/5/6.
2. **CRDT library:** yrs (max ecosystem, binary-compatible, the recommendation) vs Loro (2-5x smaller encoding — matters for mobile sync + storing agent edit history in skmem-pg). Lock yrs as primary, Loro as a watch item — confirm?
3. **Recording default:** is "minutes by default on any recorded room" the desired UX, or opt-in-per-call? (Affects the AutoEgress wiring.)
4. **Egress hardware:** where does the dedicated CPU egress worker live? Not .100 (VRAM/CPU constrained). .158? .41 (shared with Jarvis)? A new box? This is a real ops/budget decision for Phase 6.
5. **Bot install policy:** Talk makes bot install CLI/admin-only by design. Confirm Lumina + the recording/summary agent register via CLI with per-bot secrets, and that we never expose self-serve bot registration.
6. **Public exposure gate:** Phase 3 calls + Phase 4 location both touch the public-funnel/guest path. Confirm the hard gate: **no public/guest Spaces exposure until S5 capauth verification of operator assertions is done** (`/spaces/create` currently trusts the tailnet).
7. **Tier-B widgets:** do we want to reserve the iframe/WebView + capauth-scoped external-module path now (forward-compatible manifest fields), or commit fully first-party-only for the foreseeable future?
8. **Module default-enable set per agent identity:** which modules ship enabled for lumina vs jarvis vs operator vs guest? (Drives `module_prefs` seed + the `modules` block in capabilities.)

---

## Files/repos to create or touch (index)

- **Module spine:** `skchat-app/lib/core/modules/module_manifest.dart`, `module_registry.dart`, `module_context.dart`; `skchat-app/lib/services/module_prefs.dart`; refactor `lib/features/shell/app_shell.dart`, `lib/core/router/app_router.dart`, `lib/features/hub/hub_screen.dart`; new Modules tab under `lib/features/profile/`.
- **Backend discovery:** `skcomms/src/skcomms/capabilities.py` (add `api` + `modules`).
- **Typed messages:** `skchat/src/skchat/models.py` (ContentType), per-module plugins via `skchat/src/skchat/plugins.py` (`skchat.plugins` entry points).
- **Calls:** `skchat/src/skchat/livekit_routes.py`, `skchat/src/skchat/spaces/{routes,roles,moderation,tokens}.py`, `skchat-app/lib/features/{calls,spaces}/` (video grid, call-link object, call dock).
- **Location:** new `skchat` geo route + GeoStore (`/api/v1/geo/units` + publish), `skchat/src/skchat/plugins_*` location plugin, `skchat-app/lib/features/skmap/{geo_units_source.dart, skmap_providers.dart}`.
- **Docs:** yrs + `flutter_rust_bridge` core, `skchat/src/skchat/spaces/lanes.py` (Yjs channel), `skchat-app/lib/features/spaces/doc_panel.dart`, doc MCP tools in `skchat/src/skchat/mcp_server.py`.
- **Minutes:** LiveKit Egress service deploy + Redis; `skchat/src/skchat/spaces/{recording,recording_writeup,consent}.py`.
- **Specs:** one per phase under `docs/superpowers/specs/`.

This is the plan we execute from — Phase 0+1 first (module spine proven on the skmap pilot + typed-message foundation), then strictly in order, with the E2EE decision (Q1) resolved before Phase 3.
---

## DECISIONS (locked by Chef, 2026-06-23)
1. **E2EE = hybrid** — no-E2EE on the sovereign tailnet (so Lumina-as-participant + recording work); E2EE auto-enabled only for public/funnel/guest rooms.
2. **Recording → minutes = opt-in per call** (not default-on). AutoEgress only when the operator enables recording for that call.
3. **Egress worker = .158 for now** (revisit if CPU/Chrome load competes with the live services).
4. **Public-exposure gate = YES** — no public/guest room exposure until S5 capauth verification of operator assertions (`/spaces/create` currently trusts the tailnet).
5. **Start = Phase 0 (module spine) + Phase 1 (hardened 1:1 chat).**
- Defaults taken: CRDT=yrs (Loro watch), bots register CLI-only, Tier-B widgets = reserve manifest fields (build first-party only for now).

## ADDENDA (2026-06-23)

### Office editing — Collabora vs CRDT (the two-track answer)
Two different needs, two different tools:
- **Collabora Online (CODE)** — self-hosted LibreOffice-in-the-browser, real-time **human** collaborative editing of **.docx/.xlsx/.pptx/.odf** via the **WOPI** protocol (this is exactly what "Nextcloud Office"/richdocuments wraps). Best-of-breed for *real office documents* (formatted docs, spreadsheets, presentations) + viewing the office files already in skos Files. Heavy (a LibreOffice server, Docker) but self-hostable/sovereign.
  - **Weakness:** it is a closed LibreOffice editing canvas — there is **no clean API for an AI agent to co-edit inside it** (you'd have to drive UNO/macros, clunky). So Collabora is *not* the surface for "Lumina drafts side-by-side with a live cursor."
- **Yjs/yrs CRDT editor** (Phase 5) — our custom rich-text/block editor where **Lumina is a first-class CRDT peer** (live cursor, suggestion-mode). Best for agent-in-the-loop drafting; *not* office-format-native.

**Decision (recommended): run BOTH as separate modules.**
- `office` module = **Collabora/CODE via WOPI** → view + human-collab-edit .docx/.xlsx/.pptx in skos Files and in rooms. (Sovereign Docker deploy; gated by a new `office` capability/service.)
- `docs` module (Phase 5) = **Yjs/CRDT** → Lumina-collaborative drafting.
- **Bridge:** CRDT doc ⇄ office format via **LibreOffice headless** (`soffice --convert-to docx`) for import/export, so an agent-drafted doc can become a real .docx and a .docx can be opened for human collab in Collabora. Lumina drafts in CRDT → export to Collabora for polish, or vice-versa.
- Collabora is a **Phase-5+ / Files-module** concern — does NOT block Phase 0/1.

### ZIP / archive browsing (skos Files) — stream-don't-extract
Goal (Chef): browse *into* a zip on the fly, view/edit a single file inside a multi-GB archive without extracting the whole thing.
**Best-practice design (zip central directory is at the END → cheap at any size):**
- `zip_list(path)` — read only the central directory → entry list `{name, size, compressed, is_dir, mtime}`. O(1) in archive size; works on a 5 GB zip instantly.
- **Browse-as-virtual-folder:** in the Flutter browser a `.zip` is a container — tap to descend into it (a `View` over zip entries), entries render like files.
- `zip_read(path, entry)` / stream `GET /media/file?path=...&zip_entry=...` — seek to that entry's local header, decompress **only that one entry**. Viewing an image/video inside the zip uses the existing media viewer. **Confirm before opening an entry > 50 MB** (memory) — exactly Chef's ask.
- **Write-back without full re-zip:** edit one entry → **append** the new (compressed) entry at the end + rewrite only the **central directory** (small, at the end) so it points to the new copy; old entry becomes orphaned dead-space reclaimable on a later "repack". O(edited-entry) not O(archive). Confirmed action; warns + offers repack for big archives.
- All paths (archive + entry) validated through `_resolve_checked`; write-back is scope=write + audited. Same model extends to .tar (index-on-open) later.
- Slots in as a **Files-module** capability (a `MediaKind.archive` + zip `View` + the tools above).
