import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:skchat/core/theme/sovereign_colors.dart';

void main() {
  group('SovereignColors constants', () {
    test('surfaceBase is pure black (OLED)', () {
      expect(SovereignColors.surfaceBase, const Color(0xFF000000));
    });

    test('soul colors are defined', () {
      expect(SovereignColors.soulLumina, isNotNull);
      expect(SovereignColors.soulJarvis, isNotNull);
      expect(SovereignColors.soulChef, isNotNull);
    });

    test('soul colors are distinct', () {
      expect(SovereignColors.soulLumina, isNot(SovereignColors.soulJarvis));
      expect(SovereignColors.soulJarvis, isNot(SovereignColors.soulChef));
      expect(SovereignColors.soulLumina, isNot(SovereignColors.soulChef));
    });

    test('accent colors are defined', () {
      expect(SovereignColors.accentEncrypt, isNotNull);
      expect(SovereignColors.accentDanger, isNotNull);
      expect(SovereignColors.accentWarning, isNotNull);
    });
  });

  group('SovereignColors.fromFingerprint', () {
    test('produces a valid color', () {
      final color = SovereignColors.fromFingerprint('test-fingerprint');
      expect(color, isA<Color>());
      expect(color.a, 1.0);
    });

    test('same fingerprint always produces same color', () {
      const fp = 'CCBE9306410CF8CD5E393D6DEC31663B95230684';
      final color1 = SovereignColors.fromFingerprint(fp);
      final color2 = SovereignColors.fromFingerprint(fp);
      expect(color1, equals(color2));
    });

    test('different fingerprints produce different colors', () {
      final c1 = SovereignColors.fromFingerprint('fingerprint-a');
      final c2 = SovereignColors.fromFingerprint('fingerprint-b');
      // Very unlikely to be the same with different inputs.
      expect(c1, isNot(equals(c2)));
    });

    test('empty string produces a valid color', () {
      final color = SovereignColors.fromFingerprint('');
      expect(color, isA<Color>());
    });

    test('uses HSL with 70% saturation and 55% lightness', () {
      // Verify the color is in the expected range by checking it's not
      // fully saturated or desaturated.
      final color = SovereignColors.fromFingerprint('lumina');
      final hsl = HSLColor.fromColor(color);

      // Allow small floating-point tolerance.
      expect(hsl.saturation, closeTo(0.70, 0.02));
      expect(hsl.lightness, closeTo(0.55, 0.02));
      expect(hsl.alpha, 1.0);
    });

    test('hue wraps within 0-360 range', () {
      // Test with various inputs to verify hue stays in valid range.
      for (final fp in ['a', 'bb', 'ccc', 'dddd', '12345']) {
        final color = SovereignColors.fromFingerprint(fp);
        final hsl = HSLColor.fromColor(color);
        expect(hsl.hue, greaterThanOrEqualTo(0));
        expect(hsl.hue, lessThan(360));
      }
    });
  });
}
