import 'package:flutter/material.dart';
import 'package:intl/intl.dart';

import '../../core/theme/sovereign_glass.dart';
import '../../core/theme/soul_color.dart';
import '../../models/chat_message.dart';

class MessageBubble extends StatelessWidget {
  final ChatMessage message;
  final Color soulColor;
  final bool isOutbound;

  const MessageBubble({
    super.key,
    required this.message,
    required this.soulColor,
    required this.isOutbound,
  });

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        mainAxisAlignment:
            isOutbound ? MainAxisAlignment.end : MainAxisAlignment.start,
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          if (!isOutbound) const SizedBox(width: 8),
          Flexible(
            child: Column(
              crossAxisAlignment:
                  isOutbound ? CrossAxisAlignment.end : CrossAxisAlignment.start,
              children: [
                Container(
                  constraints: BoxConstraints(
                    maxWidth: MediaQuery.of(context).size.width * 0.75,
                  ),
                  decoration: BoxDecoration(
                    color: isOutbound
                        ? SoulColor.glassTint(soulColor)
                        : SovereignGlassTheme.surfaceGlass,
                    borderRadius: BorderRadius.only(
                      topLeft: const Radius.circular(16),
                      topRight: const Radius.circular(16),
                      bottomLeft: Radius.circular(isOutbound ? 16 : 4),
                      bottomRight: Radius.circular(isOutbound ? 4 : 16),
                    ),
                    border: Border.all(
                      color: isOutbound
                          ? soulColor.withValues(alpha: 0.2)
                          : SovereignGlassTheme.surfaceGlassBorder,
                      width: 1,
                    ),
                  ),
                  child: Stack(
                    children: [
                      if (!isOutbound)
                        Positioned(
                          left: 0,
                          top: 0,
                          bottom: 0,
                          child: Container(
                            width: 3,
                            decoration: BoxDecoration(
                              color: SoulColor.accentLine(soulColor),
                              borderRadius: const BorderRadius.only(
                                topLeft: Radius.circular(16),
                                bottomLeft: Radius.circular(16),
                              ),
                            ),
                          ),
                        ),
                      Padding(
                        padding: EdgeInsets.fromLTRB(
                          isOutbound ? 12 : 15,
                          12,
                          12,
                          12,
                        ),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              message.content,
                              style: const TextStyle(
                                fontSize: 15,
                                height: 1.4,
                                color: SovereignGlassTheme.textPrimary,
                              ),
                            ),
                            const SizedBox(height: 4),
                            Row(
                              mainAxisSize: MainAxisSize.min,
                              children: [
                                Text(
                                  DateFormat('h:mm a').format(message.timestamp),
                                  style: const TextStyle(
                                    fontSize: 11,
                                    color: SovereignGlassTheme.textTertiary,
                                  ),
                                ),
                                if (isOutbound) ...[
                                  const SizedBox(width: 4),
                                  Icon(
                                    _getStatusIcon(),
                                    size: 14,
                                    color: message.status == MessageStatus.read
                                        ? soulColor
                                        : SovereignGlassTheme.textTertiary,
                                  ),
                                ],
                              ],
                            ),
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
                if (message.reactions != null && message.reactions!.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 4),
                    child: Wrap(
                      spacing: 4,
                      children: message.reactions!
                          .map((r) => _buildReaction(r.emoji))
                          .toList(),
                    ),
                  ),
              ],
            ),
          ),
          if (isOutbound) const SizedBox(width: 8),
        ],
      ),
    );
  }

  Widget _buildReaction(String emoji) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
      decoration: BoxDecoration(
        color: SovereignGlassTheme.surfaceGlass,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(
          color: SovereignGlassTheme.surfaceGlassBorder,
          width: 1,
        ),
      ),
      child: Text(
        emoji,
        style: const TextStyle(fontSize: 14),
      ),
    );
  }

  IconData _getStatusIcon() {
    switch (message.status) {
      case MessageStatus.sending:
        return Icons.access_time;
      case MessageStatus.sent:
        return Icons.check;
      case MessageStatus.delivered:
        return Icons.done_all;
      case MessageStatus.read:
        return Icons.done_all;
      case MessageStatus.failed:
        return Icons.error_outline;
    }
  }
}
