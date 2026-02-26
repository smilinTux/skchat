import 'package:flutter/material.dart';
import 'sovereign_colors.dart';

/// Sovereign Glass typography — Inter Variable + JetBrains Mono for code.
/// Sizes, weights, and line heights match the PRD spec exactly.
class SovereignTypography {
  SovereignTypography._();

  static const String _fontFamily = 'Inter';
  static const String _monoFamily = 'JetBrainsMono';

  static TextTheme buildTextTheme({bool dark = true}) {
    final baseColor =
        dark ? SovereignColors.textPrimary : const Color(0xFF1A1A2E);
    final mutedColor =
        dark ? SovereignColors.textSecondary : const Color(0xFF606080);
    final dimColor =
        dark ? SovereignColors.textTertiary : const Color(0xFF909090);

    return TextTheme(
      // display — 28sp 700
      displayLarge: TextStyle(
        fontFamily: _fontFamily,
        fontSize: 28,
        fontWeight: FontWeight.w700,
        height: 1.2,
        color: baseColor,
        letterSpacing: -0.5,
      ),
      // heading — 20sp 600
      titleLarge: TextStyle(
        fontFamily: _fontFamily,
        fontSize: 20,
        fontWeight: FontWeight.w600,
        height: 1.3,
        color: baseColor,
        letterSpacing: -0.3,
      ),
      titleMedium: TextStyle(
        fontFamily: _fontFamily,
        fontSize: 17,
        fontWeight: FontWeight.w600,
        height: 1.3,
        color: baseColor,
      ),
      titleSmall: TextStyle(
        fontFamily: _fontFamily,
        fontSize: 15,
        fontWeight: FontWeight.w500,
        height: 1.4,
        color: baseColor,
      ),
      // body — 15sp 400
      bodyLarge: TextStyle(
        fontFamily: _fontFamily,
        fontSize: 15,
        fontWeight: FontWeight.w400,
        height: 1.5,
        color: baseColor,
      ),
      bodyMedium: TextStyle(
        fontFamily: _fontFamily,
        fontSize: 14,
        fontWeight: FontWeight.w400,
        height: 1.5,
        color: baseColor,
      ),
      bodySmall: TextStyle(
        fontFamily: _fontFamily,
        fontSize: 13,
        fontWeight: FontWeight.w400,
        height: 1.5,
        color: mutedColor,
      ),
      // caption — 12sp 400
      labelSmall: TextStyle(
        fontFamily: _fontFamily,
        fontSize: 12,
        fontWeight: FontWeight.w400,
        height: 1.4,
        color: dimColor,
        letterSpacing: 0.1,
      ),
      labelMedium: TextStyle(
        fontFamily: _fontFamily,
        fontSize: 13,
        fontWeight: FontWeight.w500,
        height: 1.4,
        color: mutedColor,
      ),
      labelLarge: TextStyle(
        fontFamily: _fontFamily,
        fontSize: 14,
        fontWeight: FontWeight.w600,
        height: 1.4,
        color: baseColor,
      ),
    );
  }

  /// Mono style used for fingerprints and code blocks.
  static TextStyle mono({double fontSize = 13, Color? color}) => TextStyle(
    fontFamily: _monoFamily,
    fontSize: fontSize,
    fontWeight: FontWeight.w400,
    height: 1.5,
    color: color,
  );
}
