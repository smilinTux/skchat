import 'dart:ui';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../../../core/theme/theme.dart';

/// Emoji options shown in the reaction picker.
const List<String> _kReactionEmojis = ['❤️', '🔥', '👍', '😂', '😮', '😢', '🙏'];

/// Shows a frosted-glass row of emoji reactions anchored to [anchorRect].
///
/// Call [showReactionPicker] from a long-press handler on a message bubble.
/// The [onSelect] callback receives the chosen emoji string.
///
/// The picker is presented as a route so it auto-dismisses on outside tap.
Future<void> showReactionPicker({
  required BuildContext context,
  required Rect anchorRect,
  required Color soulColor,
  required void Function(String emoji) onSelect,
}) {
  HapticFeedback.heavyImpact();
  return Navigator.of(context, rootNavigator: true).push(
    _ReactionPickerRoute(
      anchorRect: anchorRect,
      soulColor: soulColor,
      onSelect: onSelect,
    ),
  );
}

// ---------------------------------------------------------------------------
// Internal route

class _ReactionPickerRoute extends PopupRoute<void> {
  _ReactionPickerRoute({
    required this.anchorRect,
    required this.soulColor,
    required this.onSelect,
  });

  final Rect anchorRect;
  final Color soulColor;
  final void Function(String emoji) onSelect;

  @override
  Color? get barrierColor => Colors.transparent;

  @override
  bool get barrierDismissible => true;

  @override
  String? get barrierLabel => 'Dismiss reaction picker';

  @override
  Duration get transitionDuration => const Duration(milliseconds: 200);

  @override
  Widget buildPage(
    BuildContext context,
    Animation<double> animation,
    Animation<double> secondaryAnimation,
  ) {
    return _ReactionPickerOverlay(
      animation: animation,
      anchorRect: anchorRect,
      soulColor: soulColor,
      onSelect: (emoji) {
        Navigator.of(context, rootNavigator: true).pop();
        onSelect(emoji);
      },
    );
  }
}

// ---------------------------------------------------------------------------
// Overlay widget

class _ReactionPickerOverlay extends StatelessWidget {
  const _ReactionPickerOverlay({
    required this.animation,
    required this.anchorRect,
    required this.soulColor,
    required this.onSelect,
  });

  final Animation<double> animation;
  final Rect anchorRect;
  final Color soulColor;
  final void Function(String emoji) onSelect;

  @override
  Widget build(BuildContext context) {
    final screenSize = MediaQuery.of(context).size;

    // Preferred picker width
    const pickerWidth = 300.0;
    const pickerHeight = 56.0;
    const verticalOffset = 8.0;

    // Horizontal: center over the anchor, clamp to screen edges
    double dx =
        anchorRect.center.dx - pickerWidth / 2;
    dx = dx.clamp(8.0, screenSize.width - pickerWidth - 8.0);

    // Vertical: prefer above the anchor; fall back below if not enough room
    double dy = anchorRect.top - pickerHeight - verticalOffset;
    if (dy < MediaQuery.of(context).padding.top + 8) {
      dy = anchorRect.bottom + verticalOffset;
    }

    return Stack(
      children: [
        // Tap-away dismissal
        Positioned.fill(
          child: GestureDetector(
            behavior: HitTestBehavior.opaque,
            onTap: () => Navigator.of(context, rootNavigator: true).pop(),
          ),
        ),

        // Picker row
        Positioned(
          left: dx,
          top: dy,
          width: pickerWidth,
          height: pickerHeight,
          child: ScaleTransition(
            scale: CurvedAnimation(
              parent: animation,
              curve: Curves.easeOutBack,
            ),
            child: FadeTransition(
              opacity: animation,
              child: _PickerRow(
                soulColor: soulColor,
                onSelect: onSelect,
              ),
            ),
          ),
        ),
      ],
    );
  }
}

// ---------------------------------------------------------------------------
// The actual pill row

class _PickerRow extends StatelessWidget {
  const _PickerRow({
    required this.soulColor,
    required this.onSelect,
  });

  final Color soulColor;
  final void Function(String emoji) onSelect;

  @override
  Widget build(BuildContext context) {
    return ClipRRect(
      borderRadius: BorderRadius.circular(32),
      child: BackdropFilter(
        filter: ImageFilter.blur(sigmaX: 16, sigmaY: 16),
        child: Container(
          decoration: BoxDecoration(
            color: SovereignColors.surfaceRaised.withValues(alpha: 0.92),
            borderRadius: BorderRadius.circular(32),
            border: Border.all(
              color: soulColor.withValues(alpha: 0.25),
              width: 1,
            ),
            boxShadow: [
              BoxShadow(
                color: soulColor.withValues(alpha: 0.12),
                blurRadius: 20,
                spreadRadius: 0,
              ),
            ],
          ),
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
          child: Row(
            mainAxisAlignment: MainAxisAlignment.spaceEvenly,
            children: _kReactionEmojis
                .map((emoji) => _EmojiButton(
                      emoji: emoji,
                      soulColor: soulColor,
                      onTap: () => onSelect(emoji),
                    ))
                .toList(),
          ),
        ),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Individual emoji button with spring-scale animation

class _EmojiButton extends StatefulWidget {
  const _EmojiButton({
    required this.emoji,
    required this.soulColor,
    required this.onTap,
  });

  final String emoji;
  final Color soulColor;
  final VoidCallback onTap;

  @override
  State<_EmojiButton> createState() => _EmojiButtonState();
}

class _EmojiButtonState extends State<_EmojiButton>
    with SingleTickerProviderStateMixin {
  late AnimationController _controller;
  late Animation<double> _scale;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 400),
    );
    // Scale overshoot: 1.0 → 1.35 → 1.0 as per PRD "reaction added" animation
    _scale = TweenSequence<double>([
      TweenSequenceItem(
        tween: Tween(begin: 1.0, end: 1.35)
            .chain(CurveTween(curve: Curves.easeOut)),
        weight: 50,
      ),
      TweenSequenceItem(
        tween: Tween(begin: 1.35, end: 1.0)
            .chain(CurveTween(curve: Curves.elasticOut)),
        weight: 50,
      ),
    ]).animate(_controller);
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  void _handleTap() {
    HapticFeedback.mediumImpact();
    _controller.forward(from: 0).then((_) => widget.onTap());
  }

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: _handleTap,
      behavior: HitTestBehavior.opaque,
      child: ScaleTransition(
        scale: _scale,
        child: SizedBox(
          width: 36,
          height: 36,
          child: Center(
            child: Text(widget.emoji, style: const TextStyle(fontSize: 22)),
          ),
        ),
      ),
    );
  }
}
