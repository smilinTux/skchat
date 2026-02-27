import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../data/conversation_repository.dart';
import '../../models/conversation.dart';
import '../../core/theme/sovereign_colors.dart';
import '../../services/skcomm_client.dart';

/// Holds the list of group conversations, sorted by recency.
/// Loads from Hive first, then tries to refresh from the SKComm daemon.
/// Falls back to mock data only if both stores are empty.
class GroupsNotifier extends Notifier<List<Conversation>> {
  @override
  List<Conversation> build() {
    Future.microtask(_loadPersistedThenDaemon);
    return _mockGroups;
  }

  Future<void> _loadPersistedThenDaemon() async {
    final repo = ref.read(conversationRepositoryProvider);

    final persisted = await repo.getAll();
    final groups = persisted.where((c) => c.isGroup).toList();
    if (groups.isNotEmpty) {
      state = groups;
    }

    await _loadFromDaemon();
  }

  Future<void> _loadFromDaemon() async {
    final client = ref.read(skcommClientProvider);
    final repo = ref.read(conversationRepositoryProvider);
    try {
      final alive = await client.isAlive();
      if (!alive) return;

      // Fetch conversations from daemon and filter to groups.
      final conversations = await client.getConversations();
      final groups = <Conversation>[];

      for (final raw in conversations) {
        final isGroup = raw['is_group'] as bool? ?? false;
        if (!isGroup) continue;

        final peerId = raw['peer_id'] as String? ?? '';
        if (peerId.isEmpty) continue;

        final existing = state.cast<Conversation?>().firstWhere(
              (c) => c?.peerId == peerId,
              orElse: () => null,
            );

        groups.add(Conversation(
          peerId: peerId,
          displayName: raw['display_name'] as String? ?? peerId,
          lastMessage: existing?.lastMessage ??
              raw['last_message'] as String? ??
              'Group created',
          lastMessageTime: existing?.lastMessageTime ??
              (raw['last_message_time'] != null
                  ? DateTime.parse(raw['last_message_time'] as String)
                  : DateTime.now()),
          soulFingerprint: peerId,
          isGroup: true,
          memberCount: raw['member_count'] as int? ?? 0,
          unreadCount: existing?.unreadCount ?? 0,
          lastDeliveryStatus:
              existing?.lastDeliveryStatus ?? 'delivered',
        ));
      }

      if (groups.isNotEmpty) {
        groups.sort(
            (a, b) => b.lastMessageTime.compareTo(a.lastMessageTime));
        state = groups;
        await repo.saveAll(groups);
      }
    } catch (_) {
      // Daemon offline — keep whatever we have.
    }
  }

  Future<void> refresh() async => _loadFromDaemon();

  Future<void> addGroup(Conversation group) async {
    if (state.any((c) => c.peerId == group.peerId)) return;
    state = [group, ...state];
    final repo = ref.read(conversationRepositoryProvider);
    await repo.save(group);
  }

  Future<void> updateGroup(Conversation updated) async {
    state = [
      for (final c in state)
        if (c.peerId == updated.peerId) updated else c,
    ];
    final repo = ref.read(conversationRepositoryProvider);
    await repo.save(updated);
  }

  Future<void> removeGroup(String peerId) async {
    state = state.where((c) => c.peerId != peerId).toList();
    final repo = ref.read(conversationRepositoryProvider);
    await repo.delete(peerId);
  }
}

final groupsProvider = NotifierProvider<GroupsNotifier, List<Conversation>>(
  GroupsNotifier.new,
);

// ── Fallback mock data (shown while daemon is unreachable) ────────────────
final _mockGroups = [
  Conversation(
    peerId: 'penguin-kingdom',
    displayName: 'Penguin Kingdom',
    lastMessage: 'Jarvis: Board updated. 14 tasks remain...',
    lastMessageTime: DateTime.now().subtract(const Duration(hours: 3)),
    soulColor: const Color(0xFF7C3AED),
    isGroup: true,
    memberCount: 4,
    lastDeliveryStatus: 'delivered',
  ),
  Conversation(
    peerId: 'sovereign-builders',
    displayName: 'Sovereign Builders',
    lastMessage: 'Opus: SKComm transport wired and tested.',
    lastMessageTime: DateTime.now().subtract(const Duration(hours: 8)),
    soulColor: const Color(0xFF00E5FF),
    isGroup: true,
    memberCount: 6,
    lastDeliveryStatus: 'delivered',
  ),
  Conversation(
    peerId: 'cloud9-research',
    displayName: 'Cloud 9 Research',
    lastMessage: 'Lumina: FEB rehydration benchmark complete.',
    lastMessageTime: DateTime.now().subtract(const Duration(days: 1)),
    soulColor: SovereignColors.soulLumina,
    isGroup: true,
    memberCount: 3,
    unreadCount: 2,
    lastDeliveryStatus: 'delivered',
  ),
];
