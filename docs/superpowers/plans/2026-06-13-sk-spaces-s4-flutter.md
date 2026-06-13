# SK Spaces — S4 Flutter Space UI Implementation Plan

> **For agentic workers:** This plan runs in the **skchat-app** repo on **.41** (laptop), where Flutter 3.41.2 (snap: `/snap/bin/flutter`) and the app live. Dev loop: edit Dart → `cd ~/clawd/skcapstone-repos/skchat-app && /snap/bin/flutter analyze` (≈110s) → fix → commit. Reuse the existing `LiveKitCallService`; do NOT reinvent LiveKit wiring.

**Goal:** Bring SK Spaces into the consolidated Flutter app — a live-now directory + an audio-room screen (speaker rings, listener count, raise-hand, host controls, ● REC), reusing the app's `LiveKitCallService` for media and hitting the webui `/spaces` API for role-scoped tokens.

**Architecture:** A `SpacesService` (Dio) calls the webui `/spaces` endpoints (list/create/join/join-host/raise-hand/invite/remove/mute/end/record). It returns a role-scoped LiveKit token + ws url; the room screen hands that token to `LiveKitCallService.connectWithToken(...)` (a small new method — the existing `joinRoom` mints its own generic token, which we bypass for role-scoped Space tokens). Riverpod for state, GoRouter for nav — matching the app's existing patterns.

**Baseline (captured 2026-06-13):** `flutter analyze` → 25 issues, **0 errors** (lint nits only). The app already has `LiveKitCallService`, `daemon_config` (URL config), GoRouter shell nav, Riverpod, Dio.

**Endpoints (webui, base = the SKChat web-UI URL, e.g. `https://noroc2027.tail204f0c.ts.net`):**
- `GET /spaces` → `{spaces:[{space_id,title,host_fqid,status,speakers,recording}]}`
- `POST /spaces/create {host_fqid,title,slug}` → `{space_id,room,url,identity,role,token,title}`
- `POST /spaces/{id}/join {identity,name}` → listener token (same shape)
- `POST /spaces/{id}/join-host {requester}` → host token
- `POST /spaces/{id}/raise-hand {identity}` → `{ok,on_stage}`
- `POST /spaces/{id}/invite {requester,identity}` · `/remove-from-stage` · `/mute {…,track_sid}` · `/kick` · `/end {requester}`
- `POST /spaces/{id}/consent {identity}` · `/record/start {requester}` · `/record/stop {requester}`

---

## Task 1: `SpacesService` (data layer) + models

**Files (in skchat-app):**
- Create: `lib/services/spaces_service.dart`
- Create: `lib/features/spaces/space_models.dart`

**Models** (`space_models.dart`): `SpaceSummary {spaceId,title,hostFqid,status,speakers:List<String>,recording}` (+ `fromJson`); `SpaceJoin {spaceId,room,url,identity,role,token,title}` (+ `fromJson`).

**Service** (`spaces_service.dart`): a class taking a `Dio` + the webui base URL (reuse the same base-URL provider the app uses for the webui; default to `daemon`/webui host). Methods, each a thin POST/GET returning the parsed model or throwing on non-2xx:
`listLive()→List<SpaceSummary>`, `create({hostFqid,title,slug})→SpaceJoin`, `joinListener(id,{identity,name})→SpaceJoin`, `joinHost(id,{requester})→SpaceJoin`, `raiseHand(id,{identity})→bool onStage`, `invite(id,{requester,identity})`, `removeFromStage(id,{requester,identity})`, `mute(id,{requester,identity,trackSid})`, `kick(id,{requester,identity})`, `end(id,{requester})`, `consent(id,{identity})`, `recordStart(id,{requester})`, `recordStop(id,{requester})`.
Provide a Riverpod `spacesServiceProvider`.

**Verify:** `flutter analyze lib/services/spaces_service.dart lib/features/spaces/space_models.dart` → no new errors. Add a unit test `test/services/spaces_service_test.dart` using `dio` with a `MockAdapter`/`http_mock_adapter` (or a hand-rolled interceptor) asserting `listLive`/`joinListener` parse a canned JSON body and that `joinHost` posts `requester`. Run `flutter test test/services/spaces_service_test.dart`.

## Task 2: `connectWithToken` on `LiveKitCallService`

**Files:** Modify `lib/services/livekit_call_service.dart`.

The existing `joinRoom` mints a generic `/livekit/token`. Add a sibling that accepts a pre-minted role-scoped token (from `SpacesService`), reusing the same connect + listener-binding code path:
```dart
Future<void> connectWithToken({
  required String wsUrl,
  required String token,
}) async { /* construct Room, _bindRoomListeners, await room.connect(wsUrl, token), _emitParticipants */ }
```
Factor the shared connect body out of `joinRoom` so both call it (DRY). **Verify:** analyze clean; the existing call tests still pass (`flutter test`).

## Task 3: Live-now directory screen

**Files:** Create `lib/features/spaces/spaces_directory_screen.dart` + a `spacesDirectoryProvider` (FutureProvider polling `listLive()` every 5s).

Renders the 2027 design (the app's `sovereign_colors` theme): a list of live Spaces — title, host, `● LIVE`, `● REC` badge when recording, speaker count, and a **Join** button → pushes the room screen. An empty state ("No live Spaces"). A **+** FAB → a create-space sheet (title + slug) calling `create(...)` then opening the room as host. **Verify:** analyze clean; a widget test that pumps the screen with a faked provider returning two summaries and finds both titles + a REC badge.

## Task 4: Space room screen (audio + roles)

**Files:** Create `lib/features/spaces/space_room_screen.dart`.

Takes a `SpaceJoin` (from join/join-host/create). On init: `LiveKitCallService.connectWithToken(wsUrl, token)`; if role==host or canPublish, `setMicEnabled(true)`. Renders from the `participants` stream: speaker rings (pulse on `isSpeaking`, teal), a listener count, and (host only) a ✋ queue from participant metadata + tap-to-invite/remove/mute/kick via `SpacesService`. A **✋ Raise hand** button (listener) → `raiseHand(...)`. A **● REC** indicator from the summary's `recording`. Leave/End buttons (End host-only → `end(...)`). Reduced-motion + a11y per the design system. **Verify:** analyze clean; a widget test pumping the screen with a fake LiveKitCallService stream (one host + N listeners) asserting the host ring + listener count render.

## Task 5: Router + nav entry

**Files:** Modify `lib/core/router/app_router.dart` (+ `AppRoutes`).

Add routes: `/spaces` (directory) and `/spaces/:id` (room, takes the `SpaceJoin` via `extra`). Add a nav entry to reach the directory (a tab or a Profile/▶ menu item). **Verify:** analyze clean; app boots in `flutter run -d linux` (or web) and the directory route loads.

## Task 6: Dep refresh hygiene + baseline lint cleanup (optional, low-risk)

Clear the 4 warnings touching code we modify (unused imports in services/tests, the `unintended_html_in_doc_comment` in `livekit_call_service.dart`). Do NOT do the broad `Radio`/deprecation migration (out of scope). **Verify:** `flutter analyze` issue count drops; 0 errors maintained.

---

## Verification & done

- `flutter analyze` → 0 errors (warnings only, ≤ baseline).
- `flutter test` → spaces service + the two widget tests pass.
- `flutter build web` succeeds (the guest/consolidated surface) and `flutter run -d linux` boots to the directory.
- Manual: against the live .158 webui, open the directory → see "SKWorld Town Hall", Join as listener, hear a speaker; create a Space as host and publish.

## Notes
- **Base URL:** the Space API lives on the **webui** (`/spaces/...`), same host as `/livekit/token`. Reuse `LiveKitCallService`'s `webuiBaseUrl` config (dart-define / settings), not the skcomms daemon URL.
- **Guest/web parity:** this is the same Flutter surface guests get via the web build (`flutter build web`), per the all-Flutter decision.
- This plan is intentionally component-level (not line-by-line TDD): Flutter widget work over a ~110s remote analyze loop is driven incrementally; each task ends with `analyze` + a focused test.
