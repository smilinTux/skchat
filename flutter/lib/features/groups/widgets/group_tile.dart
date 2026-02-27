import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import '../../../core/theme/theme.dart';
import '../../../models/conversation.dart';

/// Glass card tile representing a group conversation.
/// Shows group avatar with member count, name, last message, timestamp,
/// encryption badge, and unread count.
class GroupTile extends StatelessWidget {
  const GroupTile({
    super.key,
    required this.group,
    required this.onTap,
    this.onLongPress,
  });

  final Conversation group;
  final VoidCallback onTap;
  final VoidCallback? onLongPress;

  @override
  Widget build(BuildContext context) {
    final tt = Theme.of(context).textTheme;
    final soul = group.resolvedSoulColor;

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
      child: GestureDetector(
        onLongPress: onLongPress,
        child: GlassCard(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
          onTap: onTap,
          child: Row(
          crossAxisAlignment: CrossAxisAlignment.center,
          children: [
            // ── Group Avatar ──────────────────────────────────────────
            _GroupAvatar(soulColor: soul, memberCount: group.memberCount),
            const SizedBox(width: 12),

            // ── Main content ─────────────────────────────────────────
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  // Name + member count + timestamp row
                  Row(
                    children: [
                      Expanded(
                        child: Row(
                          children: [
                            Flexible(
                              child: Text(
                                group.displayName,
                                style: tt.titleSmall?.copyWith(
                                  fontWeight: group.unreadCount > 0
                                      ? FontWeight.w700
                                      : FontWeight.w600,
                                ),
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                              ),
                            ),
                            const SizedBox(width: 6),
                            Text(
                              '${group.memberCount}',
                              style: tt.labelSmall?.copyWith(
                                color: SovereignColors.textTertiary,
                                fontSize: 10,
                              ),
                            ),
                            Icon(
                              Icons.people_rounded,
                              size: 12,
                              color: SovereignColors.textTertiary,
                            ),
                          ],
                        ),
                      ),
                      const SizedBox(width: 8),
                      Text(
                        _formatTime(group.lastMessageTime),
                        style: tt.labelSmall?.copyWith(
                          color: group.unreadCount > 0
                              ? soul
                              : SovereignColors.textTertiary,
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 3),

                  // Last message + badges row
                  Row(
                    children: [
                      const EncryptBadge(size: 11),
                      const SizedBox(width: 4),
                      Text(
                        'Group',
                        style: tt.labelSmall?.copyWith(
                          color: SovereignColors.accentEncrypt,
                          fontSize: 10,
                        ),
                      ),
                      const SizedBox(width: 6),
                      Expanded(
                        child: Text(
                          group.lastMessage,
                          style: tt.bodySmall?.copyWith(
                            color: group.unreadCount > 0
                                ? SovereignColors.textPrimary
                                : SovereignColors.textSecondary,
                            fontWeight: group.unreadCount > 0
                                ? FontWeight.w500
                                : FontWeight.w400,
                          ),
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                        ),
                      ),
                      if (group.unreadCount > 0) ...[
                        const SizedBox(width: 8),
                        _UnreadBadge(
                          count: group.unreadCount,
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
      ),
    );
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

/// Group avatar — shows group icon with soul-color ring.
class _GroupAvatar extends StatelessWidget {
  const _GroupAvatar({required this.soulColor, required this.memberCount});

  final Color soulColor;
  final int memberCount;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 48,
      height: 48,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        border: Border.all(color: soulColor.withValues(alpha: 0.6), width: 2),
        color: soulColor.withValues(alpha: 0.12),
      ),
      child: Center(
        child: Icon(
          Icons.group_rounded,
          color: soulColor,
          size: 22,
        ),
      ),
    );
  }
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
