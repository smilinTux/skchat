import 'dart:ui';
import 'package:flutter/material.dart';

/// Soul-color derivation from CapAuth fingerprint hash
/// Each participant gets a unique color derived from their identity
class SoulColor {
  /// Derive a soul color from a CapAuth fingerprint hash
  /// 
  /// Algorithm: fingerprint_hash → HSL(hue: hash % 360, saturation: 70%, lightness: 55%)
  static Color fromFingerprint(String fingerprint) {
    // Simple hash of the fingerprint string
    int hash = _hashString(fingerprint);
    
    // Derive hue from hash (0-360)
    double hue = (hash % 360).toDouble();
    
    // Fixed saturation and lightness for vibrant, visible colors
    double saturation = 0.70;
    double lightness = 0.55;
    
    return HSLColor.fromAHSL(1.0, hue, saturation, lightness).toColor();
  }
  
  /// Predefined soul colors for known agents
  static Color get lumina => const Color(0xFF9B6FD8); // violet-rose
  static Color get jarvis => const Color(0xFF00D9FF); // electric cyan
  static Color get chef => const Color(0xFFFFB347); // amber-gold
  
  /// Get soul color by agent name (fallback to fingerprint derivation)
  static Color forAgent(String name, {String? fingerprint}) {
    switch (name.toLowerCase()) {
      case 'lumina':
        return lumina;
      case 'jarvis':
        return jarvis;
      case 'chef':
        return chef;
      default:
        return fingerprint != null 
            ? fromFingerprint(fingerprint)
            : fromFingerprint(name);
    }
  }
  
  /// Create a gradient for soul-color avatar rings
  static Gradient avatarGradient(Color soulColor, {bool isOnline = false}) {
    return SweepGradient(
      colors: [
        soulColor,
        soulColor.withValues(alpha: 0.3),
        soulColor,
      ],
    );
  }
  
  /// Create a glow effect for online status
  static List<BoxShadow> onlineGlow(Color soulColor) {
    return [
      BoxShadow(
        color: soulColor.withValues(alpha: 0.4),
        blurRadius: 12,
        spreadRadius: 2,
      ),
    ];
  }
  
  /// Simple string hashing function
  static int _hashString(String str) {
    int hash = 0;
    for (int i = 0; i < str.length; i++) {
      hash = ((hash << 5) - hash) + str.codeUnitAt(i);
      hash = hash & hash; // Convert to 32bit integer
    }
    return hash.abs();
  }
  
  /// Create a tinted glass surface with soul color
  static Color glassTint(Color soulColor, {double opacity = 0.08}) {
    return soulColor.withValues(alpha: opacity);
  }
  
  /// Create accent line color for message bubbles
  static Color accentLine(Color soulColor) {
    return soulColor.withValues(alpha: 0.8);
  }
}
