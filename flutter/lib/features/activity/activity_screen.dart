import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:intl/intl.dart';
import '../../core/theme/theme.dart';
import '../../services/skcomm_sync.dart';
import '../chats/chats_provider.dart';

// ── Activity item model ────────────────────────────────────────────────────

enum ActivityType { message, mention, reaction, keyRotation, peerJoined, system }

class ActivityItem {
  const ActivityItem({
    required this.id,
    required this.type,
    required this.title,
    required this.body,
    required this.timestamp,
    this.soulColor,
    this.peerName,
    this.peerId,
    this.isRead = false,
    this.emoji,
  });

  final String id;
  final ActivityType type;
  final String title;
  final String body;
  final DateTime timestamp;
  final Color? soulColor;
  final String? peerName;
  final String? peerId;
  final bool isRead;
  final String? emoji;

  ActivityItem copyWith({bool? isRead}) {
    return ActivityItem(
      id: id,
      type: type,
      title: title,
      body: body,
      timestamp: timestamp,
      soulColor: soulColor,
      peerName: peerName,
      peerId: peerId,
      isRead: isRead ?? this.isRead,
      emoji: emoji,
    );
  }
}

// ── Activity feed provider ─────────────────────────────────────────────────

class ActivityFeedNotifier extends Notifier<List<ActivityItem>> {
  @override
  List<ActivityItem> build() {
    // Derive activity items from conversations (recent messages).
    final chats = ref.watch(chatsProvider);
    final daemon = ref.watch(skcommSyncProvider);

    final items = <ActivityItem>[];

    // Synthetic system event for daemon status.
    if (daemon.status == DaemonStatus.online) {
      items.add(ActivityItem(
        id: 'daemon-online',
        type: ActivityType.system,
        title: 'SKComm Online',
        body: 'Transport layer active · Encrypted',
        timestamp: daemon.lastPollAt ?? DateTime.now(),
        isRead: true,
      ));
    } else if (daemon.status == DaemonStatus.offline) {
      items.add(ActivityItem(
        id: 'daemon-offline',
        type: ActivityType.system,
        title: 'SKComm Offline',
        body: daemon.errorMessage ?? 'Daemon unreachable on localhost:9384',
        timestamp: DateTime.now(),
        isRead: false,
      ));
    }

    // Derive activity from recent conversations.
    for (final conv in chats) {
      if (conv.lastMessage.isNotEmpty) {
        final isMention = conv.lastMessage.contains('@');
        final type = isMention ? ActivityType.mention : ActivityType.message;
        items.add(ActivityItem(
          id: 'msg-${conv.peerId}',
          type: type,
          title: isMention ? 'You were mentioned' : conv.displayName,
          body: conv.lastMessage,
          timestamp: conv.lastMessageTime,
          soulColor: conv.resolvedSoulColor,
          peerName: conv.displayName,
          peerId: conv.peerId,
          isRead: conv.unreadCount == 0,
        ));
      }
    }

    // Sort by recency.
    items.sort((a, b) => b.timestamp.compareTo(a.timestamp));
    return items;
  }

  void markRead(String id) {
    state = [
      for (final item in state)
        if (item.id == id) item.copyWith(isRead: true) else item,
    ];
  }

  void markAllRead() {
    state = [for (final item in state) item.copyWith(isRead: true)];
  }
}

final activityFeedProvider =
    NotifierProvider<ActivityFeedNotifier, List<ActivityItem>>(
  ActivityFeedNotifier.new,
);

// ── Filter tabs ────────────────────────────────────────────────────────────

enum ActivityFilter { all, messages, mentions, system }

final activityFilterProvider = StateProvider<ActivityFilter>(
  (_) => ActivityFilter.all,
);

// ── Activity screen ────────────────────────────────────────────────────────

/// Activity feed screen — shows notifications, mentions, reactions, and
/// system events in a chronological feed. Filter tabs at the top.
class ActivityScreen extends ConsumerWidget {
  const ActivityScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final allItems = ref.watch(activityFeedProvider);
    final filter = ref.watch(activityFilterProvider);
    final unread = allItems.where((i) => !i.isRead).length;
    final tt = Theme.of(context).textTheme;

    final filtered = _applyFilter(allItems, filter);

    return Scaffold(
      backgroundColor: SovereignColors.surfaceBase,
      appBar: _buildAppBar(context, ref, tt, unread),
      body: Column(
        children: [
          _FilterBar(
            filter: filter,
            onChanged: (f) => ref.read(activityFilterProvider.notifier).state = f,
            items: allItems,
          ),
          Expanded(
            child: filtered.isEmpty
                ? _buildEmpty(context, tt, filter)
                : _buildFeed(context, ref, filtered, tt),
          ),
        ],
      ),
    );
  }

  PreferredSizeWidget _buildAppBar(
    BuildContext context,
    WidgetRef ref,
    TextTheme tt,
    int unreadCount,
  ) {
    return AppBar(
      backgroundColor: SovereignColors.surfaceBase,
      title: Row(
        children: [
          Text('Activity', style: tt.displayLarge?.copyWith(fontSize: 24)),
          if (unreadCount > 0) ...[
            const SizedBox(width: 8),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 2),
              decoration: BoxDecoration(
                color: SovereignColors.soulLumina,
                borderRadius: BorderRadius.circular(10),
              ),
              child: Text(
                '$unreadCount',
                style: const TextStyle(
                  fontSize: 11,
                  fontWeight: FontWeight.w700,
                  color: Colors.black,
                ),
              ),
            ),
          ],
        ],
      ),
      actions: [
        if (unreadCount > 0)
          TextButton(
            onPressed: () => ref.read(activityFeedProvider.notifier).markAllRead(),
            child: Text(
              'Mark all read',
              style: tt.labelSmall?.copyWith(
                color: SovereignColors.soulLumina,
              ),
            ),
          ),
        const SizedBox(width: 4),
      ],
    );
  }

  List<ActivityItem> _applyFilter(
    List<ActivityItem> items,
    ActivityFilter filter,
  ) {
    switch (filter) {
      case ActivityFilter.all:
        return items;
      case ActivityFilter.messages:
        return items
            .where((i) => i.type == ActivityType.message)
            .toList();
      case ActivityFilter.mentions:
        return items
            .where((i) => i.type == ActivityType.mention)
            .toList();
      case ActivityFilter.system:
        return items
            .where((i) =>
                i.type == ActivityType.system ||
                i.type == ActivityType.keyRotation ||
                i.type == ActivityType.peerJoined)
            .toList();
    }
  }

  Widget _buildEmpty(
    BuildContext context,
    TextTheme tt,
    ActivityFilter filter,
  ) {
    final label = switch (filter) {
      ActivityFilter.mentions => 'No mentions yet',
      ActivityFilter.messages => 'No new messages',
      ActivityFilter.system => 'No system events',
      _ => 'All quiet — nothing new',
    };

    return Center(
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Icon(
            Icons.notifications_none_rounded,
            size: 48,
            color: SovereignColors.textTertiary,
          ),
          const SizedBox(height: 16),
          Text(label, style: tt.titleMedium),
          const SizedBox(height: 8),
          Text(
            'Activity will appear here.',
            style: tt.bodyMedium?.copyWith(
              color: SovereignColors.textSecondary,
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildFeed(
    BuildContext context,
    WidgetRef ref,
    List<ActivityItem> items,
    TextTheme tt,
  ) {
    return ListView.builder(
      padding: const EdgeInsets.only(top: 4, bottom: 100),
      itemCount: items.length,
      itemBuilder: (context, index) {
        return _ActivityTile(
          item: items[index],
          onTap: () => ref.read(activityFeedProvider.notifier).markRead(
                items[index].id,
              ),
        );
      },
    );
  }
}

// ── Filter bar ─────────────────────────────────────────────────────────────

class _FilterBar extends StatelessWidget {
  const _FilterBar({
    required this.filter,
    required this.onChanged,
    required this.items,
  });

  final ActivityFilter filter;
  final void Function(ActivityFilter) onChanged;
  final List<ActivityItem> items;

  int _countFor(ActivityFilter f) {
    switch (f) {
      case ActivityFilter.all:
        return items.where((i) => !i.isRead).length;
      case ActivityFilter.messages:
        return items
            .where((i) => i.type == ActivityType.message && !i.isRead)
            .length;
      case ActivityFilter.mentions:
        return items
            .where((i) => i.type == ActivityType.mention && !i.isRead)
            .length;
      case ActivityFilter.system:
        return items
            .where((i) =>
                (i.type == ActivityType.system ||
                    i.type == ActivityType.keyRotation ||
                    i.type == ActivityType.peerJoined) &&
                !i.isRead)
            .length;
    }
  }

  @override
  Widget build(BuildContext context) {
    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      child: Row(
        children: ActivityFilter.values.map((f) {
          final active = f == filter;
          final count = _countFor(f);
          return Padding(
            padding: const EdgeInsets.only(right: 8),
            child: GestureDetector(
              onTap: () => onChanged(f),
              child: AnimatedContainer(
                duration: const Duration(milliseconds: 200),
                padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 7),
                decoration: BoxDecoration(
                  color: active
                      ? SovereignColors.soulLumina.withValues(alpha: 0.15)
                      : SovereignColors.surfaceGlass,
                  borderRadius: BorderRadius.circular(20),
                  border: Border.all(
                    color: active
                        ? SovereignColors.soulLumina.withValues(alpha: 0.4)
                        : SovereignColors.surfaceGlassBorder,
                  ),
                ),
                child: Row(
                  children: [
                    Text(
                      _label(f),
                      style: TextStyle(
                        fontSize: 13,
                        fontWeight: active ? FontWeight.w600 : FontWeight.w400,
                        color: active
                            ? SovereignColors.soulLumina
                            : SovereignColors.textSecondary,
                      ),
                    ),
                    if (count > 0) ...[
                      const SizedBox(width: 6),
                      Container(
                        padding: const EdgeInsets.symmetric(
                          horizontal: 5,
                          vertical: 1,
                        ),
                        decoration: BoxDecoration(
                          color: SovereignColors.soulLumina,
                          borderRadius: BorderRadius.circular(8),
                        ),
                        child: Text(
                          '$count',
                          style: const TextStyle(
                            fontSize: 10,
                            fontWeight: FontWeight.w700,
                            color: Colors.black,
                          ),
                        ),
                      ),
                    ],
                  ],
                ),
              ),
            ),
          );
        }).toList(),
      ),
    );
  }

  String _label(ActivityFilter f) {
    return switch (f) {
      ActivityFilter.all => 'All',
      ActivityFilter.messages => 'Messages',
      ActivityFilter.mentions => 'Mentions',
      ActivityFilter.system => 'System',
    };
  }
}

// ── Activity tile ──────────────────────────────────────────────────────────

class _ActivityTile extends StatelessWidget {
  const _ActivityTile({required this.item, required this.onTap});

  final ActivityItem item;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final tt = Theme.of(context).textTheme;
    final soul = item.soulColor ?? _defaultSoulColor(item.type);

    return GestureDetector(
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 3),
        child: GlassCard(
          opacity: item.isRead ? 0.03 : 0.07,
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // Icon area
              _ActivityIcon(type: item.type, soulColor: soul, emoji: item.emoji),
              const SizedBox(width: 12),
              // Content
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Expanded(
                          child: Text(
                            item.title,
                            style: tt.titleSmall?.copyWith(
                              fontWeight: item.isRead
                                  ? FontWeight.w400
                                  : FontWeight.w700,
                              color: item.isRead
                                  ? SovereignColors.textSecondary
                                  : SovereignColors.textPrimary,
                            ),
                            overflow: TextOverflow.ellipsis,
                          ),
                        ),
                        const SizedBox(width: 8),
                        Text(
                          _formatTime(item.timestamp),
                          style: tt.labelSmall?.copyWith(
                            color: SovereignColors.textTertiary,
                            fontSize: 11,
                          ),
                        ),
                      ],
                    ),
                    const SizedBox(height: 3),
                    Text(
                      item.body,
                      style: tt.bodySmall?.copyWith(
                        color: item.isRead
                            ? SovereignColors.textTertiary
                            : SovereignColors.textSecondary,
                        height: 1.4,
                      ),
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                    ),
                  ],
                ),
              ),
              // Unread dot
              if (!item.isRead)
                Padding(
                  padding: const EdgeInsets.only(left: 8, top: 4),
                  child: Container(
                    width: 8,
                    height: 8,
                    decoration: BoxDecoration(
                      color: soul,
                      shape: BoxShape.circle,
                    ),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }

  String _formatTime(DateTime dt) {
    final now = DateTime.now();
    final diff = now.difference(dt);
    if (diff.inMinutes < 1) return 'now';
    if (diff.inHours < 1) return '${diff.inMinutes}m';
    if (diff.inDays < 1) return '${diff.inHours}h';
    if (diff.inDays < 7) return '${diff.inDays}d';
    return DateFormat('MMM d').format(dt);
  }

  Color _defaultSoulColor(ActivityType type) {
    return switch (type) {
      ActivityType.mention => SovereignColors.soulLumina,
      ActivityType.reaction => SovereignColors.soulJarvis,
      ActivityType.keyRotation => SovereignColors.accentWarning,
      ActivityType.peerJoined => SovereignColors.accentEncrypt,
      ActivityType.system => SovereignColors.textTertiary,
      _ => SovereignColors.soulLumina,
    };
  }
}

// ── Activity icon ──────────────────────────────────────────────────────────

class _ActivityIcon extends StatelessWidget {
  const _ActivityIcon({
    required this.type,
    required this.soulColor,
    this.emoji,
  });

  final ActivityType type;
  final Color soulColor;
  final String? emoji;

  @override
  Widget build(BuildContext context) {
    final (icon, label) = _iconData();

    return Container(
      width: 40,
      height: 40,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        color: soulColor.withValues(alpha: 0.12),
      ),
      child: Center(
        child: emoji != null
            ? Text(emoji!, style: const TextStyle(fontSize: 18))
            : Icon(icon, size: 18, color: soulColor),
      ),
    );
  }

  (IconData, String) _iconData() {
    return switch (type) {
      ActivityType.message =>
        (Icons.chat_bubble_outline_rounded, 'Message'),
      ActivityType.mention =>
        (Icons.alternate_email_rounded, 'Mention'),
      ActivityType.reaction =>
        (Icons.favorite_border_rounded, 'Reaction'),
      ActivityType.keyRotation =>
        (Icons.rotate_right_rounded, 'Key Rotation'),
      ActivityType.peerJoined =>
        (Icons.person_add_outlined, 'Peer Joined'),
      ActivityType.system =>
        (Icons.settings_outlined, 'System'),
    };
  }
}
