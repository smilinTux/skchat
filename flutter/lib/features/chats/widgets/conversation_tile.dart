import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import '../../../core/theme/theme.dart';
import '../../../models/conversation.dart';

/// Glass card tile representing a single conversation in the chat list.
/// Shows soul-color avatar, name, last message, timestamp, encryption badge,
/// delivery status, and unread count.
class ConversationTile extends StatelessWidget {
  const ConversationTile({
    super.key,
    required this.conversation,
    required this.onTap,
  });

  final Conversation conversation;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final tt = Theme.of(context).textTheme;
    final soul = conversation.resolvedSoulColor;

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
      child: GlassCard(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
        onTap: onTap,
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.center,
          children: [
            // ── Avatar ──────────────────────────────────────────────────
            SoulAvatar(
              soulColor: soul,
              initials: conversation.resolvedInitials,
              isOnline: conversation.isOnline,
              isAgent: conversation.isAgent,
              size: 48,
            ),
            const SizedBox(width: 12),

            // ── Main content ─────────────────────────────────────────────
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  // Name + timestamp row
                  Row(
                    children: [
                      Expanded(
                        child: Text(
                          conversation.displayName,
                          style: tt.titleSmall?.copyWith(
                            fontWeight: conversation.unreadCount > 0
                                ? FontWeight.w700
                                : FontWeight.w600,
                          ),
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                        ),
                      ),
                      const SizedBox(width: 8),
                      Text(
                        _formatTime(conversation.lastMessageTime),
                        style: tt.labelSmall?.copyWith(
                          color: conversation.unreadCount > 0
                              ? soul
                              : SovereignColors.textTertiary,
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 3),

                  // Last message + unread badge row
                  Row(
                    children: [
                      // Encrypt badge
                      const EncryptBadge(size: 11),
                      const SizedBox(width: 4),
                      Text(
                        conversation.isGroup ? 'Group' : 'E2E',
                        style: tt.labelSmall?.copyWith(
                          color: SovereignColors.accentEncrypt,
                          fontSize: 10,
                        ),
                      ),
                      const SizedBox(width: 6),
                      if (!conversation.isOutboundLast)
                        Expanded(
                          child: Text(
                            conversation.isTyping
                                ? _typingText(conversation)
                                : conversation.lastMessage,
                            style: tt.bodySmall?.copyWith(
                              color: conversation.isTyping
                                  ? soul.withValues(alpha: 0.8)
                                  : conversation.unreadCount > 0
                                  ? SovereignColors.textPrimary
                                  : SovereignColors.textSecondary,
                              fontStyle: conversation.isTyping
                                  ? FontStyle.italic
                                  : FontStyle.normal,
                              fontWeight: conversation.unreadCount > 0
                                  ? FontWeight.w500
                                  : FontWeight.w400,
                            ),
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                          ),
                        )
                      else
                        Expanded(
                          child: Row(
                            children: [
                              DeliveryStatus(
                                status: conversation.lastDeliveryStatus,
                                soulColor: soul,
                              ),
                              const SizedBox(width: 4),
                              Expanded(
                                child: Text(
                                  conversation.lastMessage,
                                  style: tt.bodySmall?.copyWith(
                                    color: SovereignColors.textSecondary,
                                  ),
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                ),
                              ),
                            ],
                          ),
                        ),
                      // Unread badge
                      if (conversation.unreadCount > 0) ...[
                        const SizedBox(width: 8),
                        _UnreadBadge(
                          count: conversation.unreadCount,
                          soulColor: soul,
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
    );
  }

  String _typingText(Conversation c) {
    if (c.isAgent) {
      return '${c.displayName} is composing...';
    }
    return 'typing...';
  }

  String _formatTime(DateTime time) {
    final now = DateTime.now();
    final diff = now.difference(time);
    if (diff.inMinutes < 1) return 'now';
    if (diff.inMinutes < 60) return '${diff.inMinutes}m';
    if (diff.inHours < 24) return '${diff.inHours}h';
    if (diff.inDays < 7) return DateFormat('EEE').format(time);
    return DateFormat('MM/dd').format(time);
  }
}

extension on Conversation {
  /// Whether the last message was sent by the local user.
  /// Approximated from deliveryStatus being present without isTyping.
  bool get isOutboundLast =>
      !isTyping &&
      (lastDeliveryStatus == 'sent' ||
          lastDeliveryStatus == 'delivered' ||
          lastDeliveryStatus == 'read');
}

class _UnreadBadge extends StatelessWidget {
  const _UnreadBadge({required this.count, required this.soulColor});

  final int count;
  final Color soulColor;

  @override
  Widget build(BuildContext context) {
    return Container(
      constraints: const BoxConstraints(minWidth: 20, minHeight: 20),
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
      decoration: BoxDecoration(
        color: soulColor,
        borderRadius: BorderRadius.circular(10),
      ),
      child: Text(
        count > 99 ? '99+' : '$count',
        style: const TextStyle(
          color: Colors.black,
          fontSize: 11,
          fontWeight: FontWeight.w700,
          height: 1.2,
        ),
        textAlign: TextAlign.center,
      ),
    );
  }
}
