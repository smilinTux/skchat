import 'dart:ui';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../../../core/theme/theme.dart';
import '../../../models/chat_message.dart';
import 'reaction_picker.dart';

/// Shows a frosted-glass context menu anchored near the long-pressed message.
/// Actions: React · Reply · Copy
///
/// Call [showMessageActions] from a long-press handler on a [MessageBubble].
Future<void> showMessageActions({
  required BuildContext context,
  required ChatMessage message,
  required Rect anchorRect,
  required Color soulColor,
  VoidCallback? onReply,
  void Function(String emoji)? onReact,
}) {
  HapticFeedback.mediumImpact();
  return Navigator.of(context, rootNavigator: true).push(
    _MessageActionsRoute(
      message: message,
      anchorRect: anchorRect,
      soulColor: soulColor,
      onReply: onReply,
      onReact: onReact,
    ),
  );
}

// ---------------------------------------------------------------------------
// Internal route

class _MessageActionsRoute extends PopupRoute<void> {
  _MessageActionsRoute({
    required this.message,
    required this.anchorRect,
    required this.soulColor,
    this.onReply,
    this.onReact,
  });

  final ChatMessage message;
  final Rect anchorRect;
  final Color soulColor;
  final VoidCallback? onReply;
  final void Function(String emoji)? onReact;

  @override
  Color? get barrierColor => Colors.black.withValues(alpha: 0.28);

  @override
  bool get barrierDismissible => true;

  @override
  String? get barrierLabel => 'Dismiss actions';

  @override
  Duration get transitionDuration => const Duration(milliseconds: 180);

  @override
  Widget buildPage(
    BuildContext context,
    Animation<double> animation,
    Animation<double> secondaryAnimation,
  ) {
    // Capture the navigator reference before any pop so it remains valid
    // when we open the reaction picker after this overlay closes.
    final navigator = Navigator.of(context, rootNavigator: true);

    return _MessageActionsOverlay(
      animation: animation,
      message: message,
      anchorRect: anchorRect,
      soulColor: soulColor,
      onReply: onReply != null
          ? () {
              navigator.pop();
              onReply!();
            }
          : null,
      onReact: onReact != null
          ? () {
              navigator.pop();
              // Show the reaction picker once this overlay is fully dismissed.
              WidgetsBinding.instance.addPostFrameCallback((_) {
                showReactionPicker(
                  context: navigator.context,
                  anchorRect: anchorRect,
                  soulColor: soulColor,
                  onSelect: onReact!,
                );
              });
            }
          : null,
      onCopy: () {
        navigator.pop();
        Clipboard.setData(ClipboardData(text: message.content));
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            content: Text('Copied to clipboard'),
            duration: Duration(seconds: 2),
          ),
        );
      },
    );
  }
}

// ---------------------------------------------------------------------------
// Overlay

class _MessageActionsOverlay extends StatelessWidget {
  const _MessageActionsOverlay({
    required this.animation,
    required this.message,
    required this.anchorRect,
    required this.soulColor,
    this.onReply,
    this.onReact,
    required this.onCopy,
  });

  final Animation<double> animation;
  final ChatMessage message;
  final Rect anchorRect;
  final Color soulColor;
  final VoidCallback? onReply;
  final VoidCallback? onReact;
  final VoidCallback onCopy;

  @override
  Widget build(BuildContext context) {
    final screenSize = MediaQuery.of(context).size;
    const menuWidth = 184.0;
    const verticalOffset = 8.0;

    // Align to the bubble's edge: outbound → right-align, inbound → left-align
    double dx =
        message.isOutbound ? anchorRect.right - menuWidth : anchorRect.left;
    dx = dx.clamp(8.0, screenSize.width - menuWidth - 8.0);

    // Prefer above the bubble; fall below if not enough room
    const approxMenuHeight = 168.0;
    double dy = anchorRect.top - approxMenuHeight - verticalOffset;
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

        // Menu card
        Positioned(
          left: dx,
          top: dy,
          width: menuWidth,
          child: ScaleTransition(
            scale: CurvedAnimation(
              parent: animation,
              curve: Curves.easeOutBack,
            ),
            child: FadeTransition(
              opacity: animation,
              child: _ActionsMenu(
                soulColor: soulColor,
                onReply: onReply,
                onReact: onReact,
                onCopy: onCopy,
              ),
            ),
          ),
        ),
      ],
    );
  }
}

// ---------------------------------------------------------------------------
// Menu card

class _ActionsMenu extends StatelessWidget {
  const _ActionsMenu({
    required this.soulColor,
    this.onReply,
    this.onReact,
    required this.onCopy,
  });

  final Color soulColor;
  final VoidCallback? onReply;
  final VoidCallback? onReact;
  final VoidCallback onCopy;

  @override
  Widget build(BuildContext context) {
    return ClipRRect(
      borderRadius: BorderRadius.circular(16),
      child: BackdropFilter(
        filter: ImageFilter.blur(sigmaX: 20, sigmaY: 20),
        child: Container(
          decoration: BoxDecoration(
            color: SovereignColors.surfaceRaised.withValues(alpha: 0.94),
            borderRadius: BorderRadius.circular(16),
            border: Border.all(
              color: soulColor.withValues(alpha: 0.22),
              width: 1,
            ),
            boxShadow: [
              BoxShadow(
                color: Colors.black.withValues(alpha: 0.35),
                blurRadius: 24,
                offset: const Offset(0, 8),
              ),
            ],
          ),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (onReact != null)
                _ActionTile(
                  icon: Icons.add_reaction_outlined,
                  label: 'React',
                  soulColor: soulColor,
                  onTap: onReact!,
                  isFirst: true,
                  isLast: onReply == null,
                ),
              if (onReact != null && onReply != null)
                _Divider(soulColor: soulColor),
              if (onReply != null) ...[
                _ActionTile(
                  icon: Icons.reply_rounded,
                  label: 'Reply',
                  soulColor: soulColor,
                  onTap: onReply!,
                  isFirst: onReact == null,
                ),
                _Divider(soulColor: soulColor),
              ],
              _ActionTile(
                icon: Icons.copy_rounded,
                label: 'Copy',
                soulColor: soulColor,
                onTap: onCopy,
                isLast: true,
              ),
            ],
          ),
        ),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Tile

class _ActionTile extends StatelessWidget {
  const _ActionTile({
    required this.icon,
    required this.label,
    required this.soulColor,
    required this.onTap,
    this.isFirst = false,
    this.isLast = false,
  });

  final IconData icon;
  final String label;
  final Color soulColor;
  final VoidCallback onTap;
  final bool isFirst;
  final bool isLast;

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      behavior: HitTestBehavior.opaque,
      child: Container(
        padding: EdgeInsets.fromLTRB(16, isFirst ? 14 : 11, 16, isLast ? 14 : 11),
        child: Row(
          children: [
            Icon(icon, size: 18, color: soulColor),
            const SizedBox(width: 12),
            Text(
              label,
              style: const TextStyle(
                fontSize: 14,
                fontWeight: FontWeight.w500,
                color: SovereignColors.textPrimary,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Divider

class _Divider extends StatelessWidget {
  const _Divider({required this.soulColor});

  final Color soulColor;

  @override
  Widget build(BuildContext context) {
    return Container(height: 1, color: soulColor.withValues(alpha: 0.12));
  }
}
