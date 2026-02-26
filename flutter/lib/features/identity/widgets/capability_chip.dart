import 'package:flutter/material.dart';
import '../../../core/theme/sovereign_colors.dart';

/// Styled chip that represents a single agent capability or skill.
///
/// Uses the Sovereign Glass aesthetic: dark pill with a soul-color border
/// and optional icon prefix.
class CapabilityChip extends StatelessWidget {
  const CapabilityChip({
    super.key,
    required this.label,
    this.soulColor,
    this.icon,
    this.isActive = true,
    this.onTap,
  });

  /// The capability name displayed on the chip (e.g. "Code Review").
  final String label;

  /// Soul-color accent applied to the border and icon. Falls back to
  /// [SovereignColors.textSecondary] when null.
  final Color? soulColor;

  /// Optional leading icon inside the chip.
  final IconData? icon;

  /// Whether this capability is currently active/available. Inactive chips
  /// render at reduced opacity.
  final bool isActive;

  /// Optional tap handler. When set the chip gets a subtle ink ripple.
  final VoidCallback? onTap;

  Color get _accent => soulColor ?? SovereignColors.textSecondary;

  @override
  Widget build(BuildContext context) {
    final chip = Container(
      decoration: BoxDecoration(
        color: _accent.withValues(alpha: isActive ? 0.10 : 0.04),
        borderRadius: BorderRadius.circular(20),
        border: Border.all(
          color: _accent.withValues(alpha: isActive ? 0.35 : 0.12),
          width: 1,
        ),
      ),
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (icon != null) ...[
            Icon(
              icon,
              size: 13,
              color: _accent.withValues(alpha: isActive ? 1.0 : 0.5),
            ),
            const SizedBox(width: 5),
          ],
          Text(
            label,
            style: TextStyle(
              color: isActive
                  ? SovereignColors.textPrimary
                  : SovereignColors.textTertiary,
              fontSize: 12,
              fontWeight: FontWeight.w500,
              letterSpacing: 0.2,
            ),
          ),
        ],
      ),
    );

    if (onTap == null) {
      return Opacity(opacity: isActive ? 1.0 : 0.5, child: chip);
    }

    return Opacity(
      opacity: isActive ? 1.0 : 0.5,
      child: Material(
        color: Colors.transparent,
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(20),
          splashColor: _accent.withValues(alpha: 0.15),
          child: chip,
        ),
      ),
    );
  }
}
