import 'dart:ui';
import 'package:flutter/material.dart';
import '../../../core/theme/theme.dart';
import '../../../models/chat_message.dart';

/// Quoted-reply preview strip shown above the input bar when the user has
/// swiped to reply a message.  Jarvis will wire [replyMessage] into the
/// provider and conversation screen — this widget is purely presentational.
class ReplyPreview extends StatelessWidget {
  const ReplyPreview({
    super.key,
    required this.replyMessage,
    required this.soulColor,
    required this.onCancel,
  });

  /// The message being replied to.
  final ChatMessage replyMessage;

  /// Accent color drawn on the left edge (sender's soul-color).
  final Color soulColor;

  /// Callback fired when the user taps the cancel (×) button.
  final VoidCallback onCancel;

  @override
  Widget build(BuildContext context) {
    final tt = Theme.of(context).textTheme;
    final senderLabel = replyMessage.isOutbound
        ? 'You'
        : (replyMessage.senderName ?? 'Them');

    return ClipRRect(
      borderRadius: const BorderRadius.vertical(top: Radius.circular(12)),
      child: BackdropFilter(
        filter: ImageFilter.blur(sigmaX: 10, sigmaY: 10),
        child: Container(
          decoration: BoxDecoration(
            color: Colors.white.withValues(alpha: 0.06),
            border: Border(
              top: BorderSide(
                color: Colors.white.withValues(alpha: 0.08),
                width: 1,
              ),
            ),
          ),
          padding: const EdgeInsets.fromLTRB(0, 8, 8, 8),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // Soul-color accent bar
              Container(
                width: 3,
                height: 40,
                margin: const EdgeInsets.symmetric(horizontal: 10),
                decoration: BoxDecoration(
                  color: soulColor,
                  borderRadius: BorderRadius.circular(2),
                ),
              ),

              // Quoted content
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      senderLabel,
                      style: TextStyle(
                        fontSize: 12,
                        fontWeight: FontWeight.w600,
                        color: soulColor,
                      ),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      replyMessage.content,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: tt.labelSmall?.copyWith(
                        color: SovereignColors.textSecondary,
                      ),
                    ),
                  ],
                ),
              ),

              // Cancel button
              GestureDetector(
                onTap: onCancel,
                behavior: HitTestBehavior.opaque,
                child: Padding(
                  padding: const EdgeInsets.all(6),
                  child: Icon(
                    Icons.close,
                    size: 16,
                    color: SovereignColors.textSecondary,
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
