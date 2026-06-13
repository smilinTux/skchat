# SK 2027 Design System — skchat adoption (defers to the canonical spec)

**Date:** 2026-06-13
**★ SOURCE OF TRUTH:** `docs/skstacks-2027-design-system.md` in **smilinTux/skstacks**
(commit `5b190a2`), authored by the SKStacks session — the self-contained,
framework-agnostic *calm / flat-with-depth / transparent-AI* language extracted from
skbloom, **with a full Flutter mapping (`tokens.dart` + `skTheme` ThemeData)**.
Adoption is tracked on coord task **`d64758fb`** ("skchat Flutter UI: adopt the
SKStacks 2027 design system"). **This file is skchat's adoption notes — it does NOT
redefine the system; pull the canonical spec for the authoritative tokens/components.**

> **Correction:** an earlier draft of this file (from `skgateway/docs/DASHBOARD.md`)
> described OLED-pure-black + **frosted glass** + **purple** accent + per-agent soul
> colors. That is **WRONG / superseded** — the canonical 2027 system is **flat-with-
> depth (never glass)**, **near-black**, **one teal accent**. Use the canonical spec.

## 1. Canonical tokens (from the SKStacks spec — summary; pull `tokens.dart` for exact)
- **Canvas:** near-black `#0b0d10` (NOT pure `#000`).
- **Accent:** ONE teal `#2dd4bf → #14b8a6`. **Blue tint is reserved for "self" only.**
  **No third colour.** (So: no per-agent "soul colors" in this system — discipline.)
- **Radii:** 13 / 14 / 11. **Gap:** 14. **Elevation:** two shadow recipes (flat-with-
  depth — soft shadows, never blur/glass).
- **Type scale** per the spec; **Motion:** rise 220ms / expand 180ms / pulse 1s.
- **Flutter:** drop in `tokens.dart` + `skTheme` (M3 dark, ColorScheme, TextTheme).

## 2. The two discipline rules (guardrails — keep it coherent)
1. **Flat-with-depth, NEVER glassmorphism.**
2. **One accent (teal).** Blue = self only. No third colour. (Theming = swap the token
   block; everything derives from it.)
Plus **progressive disclosure** — don't show every control at once.

## 3. skchat-specific mappings (align to the canonical components)
- **Message row** — rise-in; "me" reversed + blue bubble (self); bot avatar = accent
  gradient. Composer; ⌘K command palette; living tiles + presence dots; expanders;
  skeleton shimmer; CTA / ghost buttons.
- **★ Transparent AI (headline fit):** when an agent is working, render the **streaming
  step-line** (what it's doing), NOT a bare "typing…". This is the marquee skchat fit.
- **skreach terminal** — the `{lane:term}` terminal lives **behind an expander** per
  this system (progressive disclosure). Matches §2.6 / Batch F3.
- **A11y:** reduced-motion gating + live regions for streaming output.

## 4. Where it applies
**Every skchat surface** — the Flutter app (`skchat-app`, all platforms incl.
web/guest), the collaborative-session UI, guest join, whiteboard chrome, the skreach
terminal pane. Add to the definition-of-done of every UI task (Batches D, F3, G).
The interim `livekit.html` should also move toward these tokens (flat-with-depth, teal,
near-black) — NOT the glass/purple it has now.

## 5. Client consolidation (unchanged, still valid)
All-Flutter — guests get the same Flutter view (Flutter-web). One codebase + this one
design system on every surface. See `2026-06-13-skchat-app-alignment.md` §4b.
