import 'package:flutter/material.dart';
import 'package:timeago/timeago.dart' as timeago;

import '../../core/theme/sovereign_glass.dart';
import '../../core/theme/soul_color.dart';
import '../../models/conversation.dart';
import 'soul_avatar.dart';

class ConversationTile extends StatelessWidget {
  final Conversation conversation;
  final VoidCallback onTap;

  const ConversationTile({
    super.key,
    required this.conversation,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final soulColor = SoulColor.forAgent(
      conversation.participantName,
      fingerprint: conversation.participantFingerprint,
    );

    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
      decoration: SovereignGlassTheme.glassDecoration(),
      child: Material(
        color: Colors.transparent,
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(SovereignGlassTheme.borderRadius),
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Row(
              children: [
                SoulAvatar(
                  name: conversation.participantName,
                  soulColor: soulColor,
                  isOnline: conversation.presenceStatus == PresenceStatus.online,
                  isAgent: conversation.isAgent,
                  size: 56,
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          Expanded(
                            child: Text(
                              conversation.participantName,
                              style: const TextStyle(
                                fontSize: 16,
                                fontWeight: FontWeight.w600,
                                color: SovereignGlassTheme.textPrimary,
                              ),
                            ),
                          ),
                          if (conversation.lastMessageTime != null)
                            Text(
                              _formatTime(conversation.lastMessageTime!),
                              style: const TextStyle(
                                fontSize: 12,
                                color: SovereignGlassTheme.textTertiary,
                              ),
                            ),
                        ],
                      ),
                      const SizedBox(height: 4),
                      Text(
                        conversation.lastMessage ?? 'No messages yet',
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          fontSize: 14,
                          color: SovereignGlassTheme.textSecondary,
                        ),
                      ),
                      const SizedBox(height: 6),
                      Row(
                        children: [
                          if (conversation.isEncrypted)
                            const Icon(
                              Icons.lock,
                              size: 14,
                              color: SovereignGlassTheme.accentEncrypt,
                            ),
                          if (conversation.isEncrypted)
                            const SizedBox(width: 4),
                          Text(
                            conversation.isGroup ? 'Group' : 'E2E',
                            style: const TextStyle(
                              fontSize: 11,
                              color: SovereignGlassTheme.textTertiary,
                            ),
                          ),
                          if (conversation.typingIndicator != null) ...[
                            const SizedBox(width: 8),
                            const Text(
                              ' · ',
                              style: TextStyle(
                                color: SovereignGlassTheme.textTertiary,
                              ),
                            ),
                            const SizedBox(width: 4),
                            Text(
                              conversation.typingIndicator!,
                              style: TextStyle(
                                fontSize: 11,
                                color: soulColor,
                                fontStyle: FontStyle.italic,
                              ),
                            ),
                          ],
                        ],
                      ),
                    ],
                  ),
                ),
                if (conversation.unreadCount > 0)
                  Container(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 8,
                      vertical: 4,
                    ),
                    decoration: BoxDecoration(
                      color: soulColor,
                      borderRadius: BorderRadius.circular(12),
                    ),
                    child: Text(
                      conversation.unreadCount.toString(),
                      style: const TextStyle(
                        fontSize: 12,
                        fontWeight: FontWeight.w600,
                        color: Colors.black,
                      ),
                    ),
                  ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  String _formatTime(DateTime time) {
    final now = DateTime.now();
    final difference = now.difference(time);

    if (difference.inMinutes < 60) {
      return '${difference.inMinutes}m';
    } else if (difference.inHours < 24) {
      return '${difference.inHours}h';
    } else if (difference.inDays < 7) {
      return '${difference.inDays}d';
    } else {
      return timeago.format(time, locale: 'en_short');
    }
  }
}
