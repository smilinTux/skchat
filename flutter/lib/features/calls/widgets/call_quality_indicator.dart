import 'package:flutter/material.dart';
import '../../../models/call_state.dart';
import '../../../core/theme/sovereign_colors.dart';

/// Four vertical bars showing WebRTC connection quality.
/// Derived from [CallQuality.signalBars] (0–4).
class CallQualityIndicator extends StatelessWidget {
  const CallQualityIndicator({
    super.key,
    required this.quality,
    this.size = 16.0,
    this.showLabel = false,
  });

  final CallQuality quality;
  final double size;
  final bool showLabel;

  @override
  Widget build(BuildContext context) {
    final bars = quality.signalBars.clamp(0, 4);
    final color = _barColor(bars);

    return Row(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.end,
      children: [
        Row(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: List.generate(4, (i) {
            final filled = i < bars;
            final barH = size * (0.4 + i * 0.2); // graduated heights
            return Padding(
              padding: const EdgeInsets.only(right: 2),
              child: AnimatedContainer(
                duration: const Duration(milliseconds: 400),
                curve: Curves.easeOutCubic,
                width: size * 0.22,
                height: barH,
                decoration: BoxDecoration(
                  color: filled
                      ? color
                      : SovereignColors.textTertiary.withValues(alpha: 0.4),
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
            );
          }),
        ),
        if (showLabel) ...[
          const SizedBox(width: 6),
          Text(
            _label(bars),
            style: TextStyle(
              fontSize: 10,
              color: color,
              fontWeight: FontWeight.w500,
            ),
          ),
        ],
      ],
    );
  }

  Color _barColor(int bars) {
    if (bars >= 3) return SovereignColors.accentEncrypt;
    if (bars == 2) return SovereignColors.accentWarning;
    return SovereignColors.accentDanger;
  }

  String _label(int bars) {
    switch (bars) {
      case 4:
        return 'Excellent';
      case 3:
        return 'Good';
      case 2:
        return 'Fair';
      case 1:
        return 'Poor';
      default:
        return 'No signal';
    }
  }
}
