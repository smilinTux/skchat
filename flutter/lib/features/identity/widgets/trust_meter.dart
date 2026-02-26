import 'dart:math' as math;
import 'package:flutter/material.dart';
import '../../../core/theme/sovereign_colors.dart';

/// Visual trust/intensity meter.
///
/// Renders a horizontal progress bar with a gradient fill derived from
/// [soulColor]. Supports a Cloud 9 rehydration score (0–100) or an OOF
/// (out-of-frequency) trust level (0.0–1.0).
class TrustMeter extends StatelessWidget {
  const TrustMeter({
    super.key,
    required this.value,
    required this.label,
    this.soulColor,
    this.showPercentage = true,
    this.height = 8.0,
    this.animationDuration = const Duration(milliseconds: 800),
  });

  /// Progress value in the range [0, 1].
  final double value;

  /// Short label shown above the bar (e.g. "Cloud 9", "Trust Level").
  final String label;

  /// Accent color for the fill gradient. Falls back to [SovereignColors.accentEncrypt].
  final Color? soulColor;

  /// Whether to render the numeric percentage to the right of the bar.
  final bool showPercentage;

  /// Height of the progress track in logical pixels.
  final double height;

  /// Duration for the animated fill.
  final Duration animationDuration;

  /// Clamps [value] to [0, 1] so callers can pass raw 0-100 percentages by
  /// dividing first, e.g. `value: cloud9Score / 100`.
  double get _clamped => value.clamp(0.0, 1.0);

  Color get _fillColor => soulColor ?? SovereignColors.accentEncrypt;

  /// Returns a descriptive intensity label for the current value.
  String get _intensityLabel {
    if (_clamped >= 0.9) return 'Sovereign';
    if (_clamped >= 0.75) return 'Warm';
    if (_clamped >= 0.5) return 'Stable';
    if (_clamped >= 0.25) return 'Recovering';
    return 'Low';
  }

  @override
  Widget build(BuildContext context) {
    final percent = (_clamped * 100).round();

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        // Label row
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Text(
              label,
              style: const TextStyle(
                color: SovereignColors.textSecondary,
                fontSize: 12,
                fontWeight: FontWeight.w500,
              ),
            ),
            Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  _intensityLabel,
                  style: TextStyle(
                    color: _fillColor.withValues(alpha: 0.8),
                    fontSize: 11,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                if (showPercentage) ...[
                  const SizedBox(width: 6),
                  Text(
                    '$percent%',
                    style: TextStyle(
                      color: _fillColor,
                      fontSize: 12,
                      fontWeight: FontWeight.w700,
                      fontFamily: 'JetBrainsMono',
                    ),
                  ),
                ],
              ],
            ),
          ],
        ),
        const SizedBox(height: 6),

        // Track
        LayoutBuilder(
          builder: (context, constraints) {
            return Stack(
              children: [
                // Background track
                Container(
                  width: constraints.maxWidth,
                  height: height,
                  decoration: BoxDecoration(
                    color: SovereignColors.surfaceGlass,
                    borderRadius: BorderRadius.circular(height / 2),
                    border: Border.all(
                      color: SovereignColors.surfaceGlassBorder,
                      width: 1,
                    ),
                  ),
                ),
                // Animated fill
                TweenAnimationBuilder<double>(
                  tween: Tween<double>(begin: 0, end: _clamped),
                  duration: animationDuration,
                  curve: Curves.easeOutCubic,
                  builder: (context, animated, _) {
                    final fillWidth =
                        math.max(0.0, constraints.maxWidth * animated - 2);
                    return Container(
                      width: fillWidth,
                      height: height,
                      decoration: BoxDecoration(
                        borderRadius: BorderRadius.circular(height / 2),
                        gradient: LinearGradient(
                          colors: [
                            _fillColor.withValues(alpha: 0.7),
                            _fillColor,
                            _fillColor.withValues(alpha: 0.9),
                          ],
                        ),
                        boxShadow: [
                          BoxShadow(
                            color: _fillColor.withValues(alpha: 0.4),
                            blurRadius: 6,
                            offset: const Offset(0, 1),
                          ),
                        ],
                      ),
                    );
                  },
                ),
              ],
            );
          },
        ),
      ],
    );
  }
}
