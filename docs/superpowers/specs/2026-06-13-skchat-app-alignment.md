# skchat-app (Flutter native client) — framework-alignment assessment + plan

**Date:** 2026-06-13
**App repo:** `~/clawd/skcapstone-repos/skchat-app` (on .41 / laptop)
**Status:** assessed; Batch G queued

---

## 1. Assessment — how far off?

**Not far structurally — it's a real v0.1, not a skeleton.** ~**18,900 lines of Dart**,
**6 platforms** (android/ios/linux/macos/web/windows), 50 feature files, 10 services.
Last commit `bf2e850` (v0.1.0, **2026-03-15** — ~3 months stale, predates the whole
LiveKit-unified architecture we designed tonight).

**What it already has (solid bones):**
- **Identity** — capauth/identity services, trust meter, capability chips, QR login.
- **Messaging** — `skcomm_client` + `skcomm_sync` to the SKComm daemon; chats, groups
  (create/info/tiles), conversations/messages with Hive local storage.
- **Coord board screen** — it already renders the coordination board.
- **Onboarding/pairing** — `skchat://peer/<fingerprint>` URIs + QR pairing flow,
  transport selection.
- **Calls** — `webrtc_service` does **raw `flutter_webrtc`** P2P over the **skcomms
  signaling broker** (`ws://localhost:9384/webrtc/ws?room=…`).
- State = Riverpod; HTTP = dio; storage = Hive; biometric + notifications.

**The core gap:** it predates **LiveKit**. Calls are raw-P2P-over-the-skcomms-broker
(which IS the sovereign sub-project-B/P2P tier — keep it!), but it has **no
`livekit_client`**, so it can't join the agent/collaborative rooms the web client +
Lumina/Opus use, and none of the collaborative-session lanes exist.

## 2. The alignment gap (what "tacking into the framework" means)

| Gap | Detail |
|---|---|
| **LiveKit** (the big one) | Add `livekit_client` Flutter SDK; join the deterministic per-pair/group rooms via the `/livekit/token` mint (same `derive_room` as web). Gets the app into the same rooms as the web client + agents. Keep raw P2P as the direct/sovereign tier (the ICE ladder). |
| **Collaborative session lanes** | Render the data-channel lanes — chat / whiteboard (Excalidraw equiv) / screen / term — that ride the LiveKit data channel (the "one room, many lanes" model). |
| **Voice engine / agents** | Agents join as LiveKit participants; the app just renders/plays them. Wire the agent-presence + the new voice transport. |
| **Endpoints/transports** | Re-point at the current services (webui `:8765` token mint, the new `skchat-voice` :18800, the connectivity tier ladder); align with the v2 deploy (tailnet names). |
| **Pairing ↔ guest/access** | Align the `skchat://peer` pairing + onboarding with the new **guest-join** (`guest.py`) + the **P0 identity/roles** model (member/guest tiers). |
| **Multi-conversation** | It already has chats/groups — polish to the Batch D7 conversation-list+switcher UX. |
| **Refresh** | 3-month dep staleness; bump pubspec, re-verify the 6 platform builds, add CI. |

## 3. Batch G — skchat-app alignment (queued)
- **G1** Add `livekit_client` + a `LiveKitCallService` that mints a token from
  `/livekit/token` and joins the deterministic room — alongside the existing raw-P2P
  path (tier selection: tailnet/LAN direct → LiveKit SFU for group/agent rooms).
- **G2** Data-channel **session lanes** in the app (chat panel + whiteboard +
  screen-share render + `{lane:term}` for skreach) — mirror the web `livekit.html` model.
- **G3** Re-point endpoints/transports at the current framework; align onboarding/
  pairing with `guest.py` + the P0 identity/roles model.
- **G4** Multi-conversation polish (conversation list + switcher = Batch D7) over the
  existing chats/groups.
- **G5** Dep refresh + 6-platform build verification + CI; sync the app into the
  `skchat-unified` epic structure.

## 4. Tooling note (blocker for coding)
**Flutter is NOT on PATH on .41.** Before writing/iterating Dart, set up the Flutter
SDK on .41 (or a build host) so there's an `analyze`/`build`/`test` loop — editing a
19k-LOC app blind (no build) is not safe. First concrete step is tooling + a
`flutter analyze` baseline, then G1.

## 4b. Client-surface decision (2026-06-13)

**Consolidate phone + desktop into the ONE Flutter app** — Flutter is inherently
one codebase → all targets, and `skchat-app` already builds all six. No separate
phone app; the UI adapts per form factor (phone bottom-nav/single-pane ↔ desktop
sidebar/multi-pane via responsive breakpoints / master-detail). Batch G targets
this single consolidated app.

**UPDATED 2026-06-13 (Chef: "give the guest our nice flutter app view… consolidate!"):
go ALL-FLUTTER.** Guests open the **Flutter-web build** of the same app — one codebase
for iOS/Android/Linux/macOS/Windows **and web/guest**. Tradeoff (heavier web bundle)
accepted for one codebase + identical UX; mitigate with deferred loading, a thin
pre-auth invite landing, CDN/caching. `livekit.html` stays as a fallback/dev surface
(restyled to the 2027 tokens in the interim). Full decision +
mitigations: `2026-06-13-sk-design-system-2027.md` §4.

**Design language:** every skchat surface uses the **SK 2027 design system** —
*flat-with-depth (never glass)*, near-black `#0b0d10`, ONE teal accent `#2dd4bf`
(blue = self only), Inter/JetBrains Mono, the rise/expand/pulse motion. Canonical:
`skstacks-2027-design-system.md` (smilinTux/skstacks `5b190a2`) + skchat notes in
`2026-06-13-sk-design-system-2027.md`. Add it to every UI task's DoD.

## 5. Verdict
A genuinely solid, substantial native client that's ~3 months behind the architecture.
The bones (identity, messaging, groups, coord, pairing, P2P calls, 6 platforms) are
exactly right; the work is **re-homing its call/session layer onto LiveKit + the
collaborative-session lanes**, re-pointing endpoints, and a refresh — not a rewrite.
It bolts into the framework as the **native multi-platform client** beside the web UI.
