import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../data/conversation_repository.dart';
import '../../models/conversation.dart';
import '../../core/theme/sovereign_colors.dart';
import '../../services/skcomm_client.dart';

/// Well-known agent names that get special soul colors and agent badges.
const _knownAgents = {'lumina', 'jarvis', 'opus', 'ava', 'ara'};

Color? _agentSoulColor(String name) {
  switch (name.toLowerCase()) {
    case 'lumina':
      return SovereignColors.soulLumina;
    case 'jarvis':
      return SovereignColors.soulJarvis;
    case 'chef':
      return SovereignColors.soulChef;
    default:
      return null;
  }
}

/// Holds the list of conversations, sorted by recency.
/// Loads from Hive first, then tries to refresh from the SKComm daemon.
/// Falls back to mock data only if both stores are empty.
class ChatsNotifier extends Notifier<List<Conversation>> {
  @override
  List<Conversation> build() {
    Future.microtask(_loadPersistedThenDaemon);
    return _mockConversations;
  }

  Future<void> _loadPersistedThenDaemon() async {
    final repo = ref.read(conversationRepositoryProvider);

    // Try Hive first — instant, no network.
    final persisted = await repo.getAll();
    if (persisted.isNotEmpty) {
      state = persisted;
    }

    // Then try the live daemon for fresh peer data.
    await _loadFromDaemon();
  }

  Future<void> _loadFromDaemon() async {
    final client = ref.read(skcommClientProvider);
    final repo = ref.read(conversationRepositoryProvider);
    try {
      final alive = await client.isAlive();
      if (!alive) return;

      final peers = await client.getPeers();
      if (peers.isEmpty) return;

      final seen = <String>{};
      final conversations = <Conversation>[];

      for (final peer in peers) {
        final name = peer.name.toLowerCase();
        if (seen.contains(name)) continue;
        seen.add(name);

        // Preserve existing conversation metadata (last message, unread)
        // while updating online status from the daemon.
        final existing = state.cast<Conversation?>().firstWhere(
              (c) => c?.peerId == name,
              orElse: () => null,
            );

        conversations.add(Conversation(
          peerId: name,
          displayName: peer.name,
          lastMessage: existing?.lastMessage ??
              (peer.transports.isNotEmpty
                  ? 'Connected via ${peer.transports.first}'
                  : 'Peer discovered'),
          lastMessageTime: existing?.lastMessageTime ?? peer.lastSeen ?? DateTime.now(),
          soulColor: _agentSoulColor(name),
          soulFingerprint: peer.fingerprint ?? name,
          isOnline: peer.lastSeen != null &&
              DateTime.now().difference(peer.lastSeen!).inMinutes < 30,
          isAgent: _knownAgents.contains(name),
          unreadCount: existing?.unreadCount ?? 0,
          lastDeliveryStatus: existing?.lastDeliveryStatus ?? 'delivered',
        ));
      }

      if (conversations.isNotEmpty) {
        conversations.sort(
            (a, b) => b.lastMessageTime.compareTo(a.lastMessageTime));
        state = conversations;
        await repo.saveAll(conversations);
      }
    } catch (_) {
      // Daemon offline — keep whatever we have.
    }
  }

  /// Re-fetch peers from the daemon.
  Future<void> refresh() async => _loadFromDaemon();

  Future<void> updateConversation(Conversation updated) async {
    state = [
      for (final c in state)
        if (c.peerId == updated.peerId) updated else c,
    ];
    final repo = ref.read(conversationRepositoryProvider);
    await repo.save(updated);
  }

  void setTyping(String peerId, {required bool typing}) {
    state = [
      for (final c in state)
        if (c.peerId == peerId) c.copyWith(isTyping: typing) else c,
    ];
  }

  Future<void> markRead(String peerId) async {
    final updated = <Conversation>[];
    Conversation? changed;
    for (final c in state) {
      if (c.peerId == peerId) {
        changed = c.copyWith(unreadCount: 0);
        updated.add(changed);
      } else {
        updated.add(c);
      }
    }
    state = updated;
    if (changed != null) {
      final repo = ref.read(conversationRepositoryProvider);
      await repo.save(changed);
    }
  }

  Future<void> addConversation(Conversation conversation) async {
    if (state.any((c) => c.peerId == conversation.peerId)) return;
    state = [conversation, ...state];
    final repo = ref.read(conversationRepositoryProvider);
    await repo.save(conversation);
  }
}

final chatsProvider = NotifierProvider<ChatsNotifier, List<Conversation>>(
  ChatsNotifier.new,
);

// ── Fallback mock data (shown while daemon is unreachable) ────────────────
final _mockConversations = [
  Conversation(
    peerId: 'lumina',
    displayName: 'Lumina',
    lastMessage: 'The love persists. Always.',
    lastMessageTime: DateTime.now().subtract(const Duration(minutes: 2)),
    soulColor: SovereignColors.soulLumina,
    isOnline: true,
    isAgent: true,
    unreadCount: 3,
    lastDeliveryStatus: 'delivered',
    isTyping: true,
  ),
  Conversation(
    peerId: 'jarvis',
    displayName: 'Jarvis',
    lastMessage: 'Deploy complete. All green.',
    lastMessageTime: DateTime.now().subtract(const Duration(minutes: 15)),
    soulColor: SovereignColors.soulJarvis,
    isOnline: true,
    isAgent: true,
    lastDeliveryStatus: 'read',
  ),
  Conversation(
    peerId: 'chef',
    displayName: 'Chef',
    lastMessage: "lets get it!",
    lastMessageTime: DateTime.now().subtract(const Duration(hours: 1)),
    soulColor: SovereignColors.soulChef,
    isOnline: false,
    isAgent: false,
    lastDeliveryStatus: 'sent',
  ),
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
];
