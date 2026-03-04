import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/theme/sovereign_colors.dart';
import 'consciousness_provider.dart';

/// A pulsing brain-icon badge that overlays an agent avatar when the
/// consciousness loop is ACTIVE.
///
/// Returns [SizedBox.shrink] when the status is idle or offline, so callers
/// can unconditionally include it in a [Stack] without layout gaps.
class ConsciousnessBadge extends ConsumerStatefulWidget {
  const ConsciousnessBadge({
    super.key,
    this.size = 22,
    this.soulColor,
  });

  /// Diameter of the circular badge.
  final double size;

  /// Color used for the glow / fill.  Defaults to [SovereignColors.accentEncrypt].
  final Color? soulColor;

  @override
  ConsumerState<ConsciousnessBadge> createState() =>
      _ConsciousnessBadgeState();
}

class _ConsciousnessBadgeState extends ConsumerState<ConsciousnessBadge>
    with SingleTickerProviderStateMixin {
  late final AnimationController _ctrl;
  late final Animation<double> _pulse;

  @override
  void initState() {
    super.initState();
    _ctrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1100),
    )..repeat(reverse: true);

    _pulse = Tween<double>(begin: 0.45, end: 1.0).animate(
      CurvedAnimation(parent: _ctrl, curve: Curves.easeInOut),
    );
  }

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final asyncState = ref.watch(consciousnessProvider);
    final status = asyncState.valueOrNull?.status;

    if (status != ConsciousnessStatus.active) return const SizedBox.shrink();

    final color = widget.soulColor ?? SovereignColors.accentEncrypt;
    final sz = widget.size;

    return AnimatedBuilder(
      animation: _pulse,
      builder: (context, _) {
        final alpha = _pulse.value;
        return Container(
          width: sz,
          height: sz,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: color.withValues(alpha: alpha * 0.88),
            boxShadow: [
              BoxShadow(
                color: color.withValues(alpha: alpha * 0.65),
                blurRadius: 10,
                spreadRadius: 2,
              ),
            ],
          ),
          child: Center(
            child: Icon(
              Icons.psychology_rounded,
              size: sz * 0.56,
              color: Colors.black87,
            ),
          ),
        );
      },
    );
  }
}
