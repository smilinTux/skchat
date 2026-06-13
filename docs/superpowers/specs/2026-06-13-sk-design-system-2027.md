# SK Design System ("2027 style") — apply to ALL skchat interfaces

**Date:** 2026-06-13
**Source of truth:** the SK design system as defined in
`skcapstone-repos/skgateway/docs/DASHBOARD.md` §Design System — the same language the
v2 UIs (skbloom day-2 control plane, skgateway dashboard) use. This spec adopts it as
the **mandatory design language for every skchat surface** (Flutter app + web/guest)
for fleet-wide visual consistency.

---

## 1. Design tokens

| Token | Value | Notes |
|---|---|---|
| **Background** | OLED black `#000000` (no grey) | battery-saving; max contrast for glass |
| **Surfaces** | **glass cards** — `backdrop-filter: blur(20px)` + subtle linear-gradient overlay | consistent across every panel |
| **Accent** | purple `#A855F7` | active states, highlights, logo (also = lumina's soul color) |
| **UI font** | `Inter` | all UI text |
| **Mono font** | `JetBrains Mono` | numbers, code, model/agent ids, timestamps |

## 2. Soul colors (per-agent identity — fixed across ALL SK tooling)

Each agent renders in its soul color everywhere it appears (avatar ring, message
accent, presence dot, name, call tile border, coord/activity entries).

| Agent | Hex | | Agent | Hex |
|---|---|---|---|---|
| lumina | `#A855F7` (purple) | | herald | `#10B981` (emerald) |
| jarvis | `#06B6D4` (cyan) | | architect | `#6366F1` (indigo) |
| opus | `#F59E0B` (amber) | | scholar | `#8B5CF6` (violet) |
| sentinel | `#EF4444` (red) | | steward | `#14B8A6` (teal) |
| artisan | `#EC4899` (pink) | | coder | `#F97316` (orange) |
| | | | unknown | `#64748B` (slate) |

## 3. Application per surface

**Flutter app (`skchat-app`) — primary surface, all platforms incl. web/guest.**
- `ThemeData(brightness: dark)`: `scaffoldBackgroundColor: Color(0xFF000000)`,
  `colorScheme.primary: Color(0xFFA855F7)`.
- Fonts: `Inter` (default text theme) + `JetBrains Mono` (numeric/code styles) via
  `google_fonts` or bundled assets.
- **Glass cards:** a reusable `GlassCard` widget = `ClipRRect` + `BackdropFilter`
  (`ImageFilter.blur(sigmaX:20, sigmaY:20)`) over a container with a subtle
  white-alpha linear gradient + 1px hairline border. Use for every panel.
- **Soul colors:** a single `soulColor(agentId) -> Color` map (the table above);
  used for avatars/borders/presence/message accents fleet-wide.
- Responsive: phone (bottom-nav, single-pane) ↔ desktop/tablet (sidebar, master-detail).

**Web / guest (`livekit.html` today):** until the Flutter-web build is the guest
surface (see §4), restyle to the same tokens — `body{background:#000}`,
`backdrop-filter:blur(20px)` glass cards, `#A855F7` accent, Inter/JetBrains Mono,
soul-color tiles — so it matches even in the interim.

## 4. Client-surface consolidation (UPDATED 2026-06-13 — Chef: "consolidate!")

**Supersedes the prior split decision.** Go **all-Flutter** — guests get the same
nice Flutter view. One codebase (`skchat-app`) → iOS / Android / Linux / macOS /
Windows **and web** (Flutter Web). The guest magic-link opens the **Flutter-web build**
(not a separate HTML page).
- **Tradeoff acknowledged:** Flutter-web's larger bundle / slower first paint vs a tiny
  HTML page. Mitigate with: deferred/lazy loading, a lightweight loading splash, CDN +
  caching, and a thin pre-auth landing for the invite (validate token, then load the
  app). Accept the tradeoff for one codebase + identical UX everywhere.
- `livekit.html` stays as a **fallback / dev surface** and gets the 2027 restyle in the
  interim, but the canonical guest surface becomes Flutter-web.

## 5. Mandate
Every NEW skchat interface (the collaborative session UI, guest join, terminal/skreach
pane, whiteboard chrome, the app shell) MUST use these tokens + the GlassCard +
soul-color system. Add this spec to the definition-of-done for any UI task (Batches
D, F3, G).
