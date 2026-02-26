import 'package:flutter/material.dart';
import 'dart:ui';

/// Sovereign Glass Design System
/// OLED-first dark theme with glass surfaces and soul-color theming
class SovereignGlassTheme {
  // Base color palette - Dark Mode Primary
  static const Color surfaceBase = Color(0xFF000000); // OLED black
  static const Color surfaceRaised = Color(0xFF0A0A0F); // Card backgrounds
  static const Color surfaceGlass = Color(0x0FFFFFFF); // rgba(255,255,255,0.06)
  static const Color surfaceGlassBorder = Color(0x14FFFFFF); // rgba(255,255,255,0.08)
  
  static const Color textPrimary = Color(0xFFE8E8F0);
  static const Color textSecondary = Color(0xFF808098);
  static const Color textTertiary = Color(0xFF505068);
  
  static const Color accentEncrypt = Color(0xFF10B981); // Encryption confirmed
  static const Color accentDanger = Color(0xFFEF4444); // Errors, delete
  static const Color accentWarning = Color(0xFFF59E0B); // Unverified, expiring
  
  /// Glass blur radius for BackdropFilter
  static const double glassBlurSigma = 12.0;
  
  /// Standard border radius
  static const double borderRadius = 16.0;
  
  /// Glass surface decoration builder
  static BoxDecoration glassDecoration({
    Color? color,
    double? radius,
    bool showBorder = true,
  }) {
    return BoxDecoration(
      color: color ?? surfaceGlass,
      borderRadius: BorderRadius.circular(radius ?? borderRadius),
      border: showBorder
          ? Border.all(color: surfaceGlassBorder, width: 1)
          : null,
    );
  }
  
  /// Create a glass surface widget
  static Widget glassCard({
    required Widget child,
    double? radius,
    Color? color,
    bool showBorder = true,
  }) {
    return Container(
      decoration: glassDecoration(
        color: color,
        radius: radius,
        showBorder: showBorder,
      ),
      child: ClipRRect(
        borderRadius: BorderRadius.circular(radius ?? borderRadius),
        child: BackdropFilter(
          filter: ImageFilter.blur(
            sigmaX: glassBlurSigma,
            sigmaY: glassBlurSigma,
          ),
          child: child,
        ),
      ),
    );
  }
  
  /// Dark theme configuration
  static ThemeData darkTheme() {
    return ThemeData(
      useMaterial3: true,
      brightness: Brightness.dark,
      
      // Color scheme
      colorScheme: const ColorScheme.dark(
        primary: textPrimary,
        secondary: textSecondary,
        surface: surfaceBase,
        surfaceContainerHighest: surfaceRaised,
        error: accentDanger,
        onPrimary: surfaceBase,
        onSecondary: surfaceBase,
        onSurface: textPrimary,
        onError: textPrimary,
      ),
      
      // Scaffold
      scaffoldBackgroundColor: surfaceBase,
      
      // App bar
      appBarTheme: const AppBarTheme(
        backgroundColor: Colors.transparent,
        elevation: 0,
        surfaceTintColor: Colors.transparent,
        foregroundColor: textPrimary,
        titleTextStyle: TextStyle(
          fontFamily: 'Inter',
          fontSize: 20,
          fontWeight: FontWeight.w600,
          color: textPrimary,
          letterSpacing: -0.5,
        ),
      ),
      
      // Text theme
      textTheme: const TextTheme(
        displayLarge: TextStyle(
          fontFamily: 'Inter',
          fontSize: 28,
          fontWeight: FontWeight.w700,
          height: 1.2,
          color: textPrimary,
          letterSpacing: -0.5,
        ),
        headlineMedium: TextStyle(
          fontFamily: 'Inter',
          fontSize: 20,
          fontWeight: FontWeight.w600,
          height: 1.3,
          color: textPrimary,
          letterSpacing: -0.3,
        ),
        bodyLarge: TextStyle(
          fontFamily: 'Inter',
          fontSize: 15,
          fontWeight: FontWeight.w400,
          height: 1.5,
          color: textPrimary,
        ),
        bodyMedium: TextStyle(
          fontFamily: 'Inter',
          fontSize: 13,
          fontWeight: FontWeight.w400,
          height: 1.4,
          color: textSecondary,
        ),
        labelMedium: TextStyle(
          fontFamily: 'Inter',
          fontSize: 12,
          fontWeight: FontWeight.w400,
          height: 1.4,
          color: textTertiary,
        ),
      ),
      
      // Card theme
      cardTheme: CardTheme(
        color: surfaceGlass,
        elevation: 0,
        margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(borderRadius),
          side: const BorderSide(color: surfaceGlassBorder, width: 1),
        ),
      ),
      
      // Input decoration
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: surfaceGlass,
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(borderRadius),
          borderSide: const BorderSide(color: surfaceGlassBorder),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(borderRadius),
          borderSide: const BorderSide(color: surfaceGlassBorder),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(borderRadius),
          borderSide: const BorderSide(color: textPrimary, width: 1.5),
        ),
        hintStyle: const TextStyle(
          fontFamily: 'Inter',
          fontSize: 15,
          fontWeight: FontWeight.w400,
          color: textSecondary,
        ),
      ),
      
      // Icon theme
      iconTheme: const IconThemeData(
        color: textPrimary,
        size: 24,
      ),
      
      // Divider
      dividerTheme: const DividerThemeData(
        color: surfaceGlassBorder,
        thickness: 1,
      ),
    );
  }
}
