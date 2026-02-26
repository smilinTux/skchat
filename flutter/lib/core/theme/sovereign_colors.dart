import 'package:flutter/material.dart';

/// Sovereign Glass color palette — OLED-first dark mode with glass surfaces.
/// All values derived from the PRD color spec.
class SovereignColors {
  SovereignColors._();

  // ── Surface ──────────────────────────────────────────────────────────────
  static const Color surfaceBase = Color(0xFF000000); // OLED black
  static const Color surfaceRaised = Color(0xFF0A0A0F); // card backgrounds
  static const Color surfaceGlass = Color(0x0FFFFFFF); // ~6% white opacity
  static const Color surfaceGlassBorder = Color(0x14FFFFFF); // ~8% white

  // ── Text ─────────────────────────────────────────────────────────────────
  static const Color textPrimary = Color(0xFFE8E8F0);
  static const Color textSecondary = Color(0xFF808098);
  static const Color textTertiary = Color(0xFF505068);

  // ── Accent ───────────────────────────────────────────────────────────────
  static const Color accentEncrypt = Color(0xFF10B981); // emerald green
  static const Color accentDanger = Color(0xFFEF4444); // red
  static const Color accentWarning = Color(0xFFF59E0B); // amber

  // ── Soul colors (well-known agents) ──────────────────────────────────────
  static const Color soulLumina = Color(0xFFBB86FC); // violet-rose
  static const Color soulJarvis = Color(0xFF00E5FF); // electric cyan
  static const Color soulChef = Color(0xFFFFC107); // amber-gold

  // ── Light mode surfaces ───────────────────────────────────────────────────
  static const Color surfaceBaseLight = Color(0xFFFAFAFE);
  static const Color surfaceRaisedLight = Color(0xFFF0F0F8);
  static const Color surfaceGlassLight = Color(0x0A000000); // ~4% black
  static const Color surfaceGlassBorderLight = Color(0x14000000);

  /// Derives a soul-color from a CapAuth fingerprint string.
  /// Uses the PRD formula: HSL(hue: hash % 360, sat: 70%, light: 55%)
  static Color fromFingerprint(String fingerprint) {
    final hash = fingerprint.codeUnits.fold<int>(
      0,
      (acc, c) => (acc * 31 + c) & 0x7FFFFFFF,
    );
    final hue = (hash % 360).toDouble();
    return HSLColor.fromAHSL(1.0, hue, 0.70, 0.55).toColor();
  }
}
