import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/theme/glass_decorations.dart';
import '../../core/theme/sovereign_glass.dart';
import '../../core/transport/skcomm_client.dart';
import '../../models/conversation.dart';
import 'widgets/conversation_tile.dart';

/// Well-known agent names that get special badges.
const _knownAgents = {'lumina', 'jarvis', 'opus', 'ava', 'ara'};

/// Riverpod provider that fetches conversations from the SKComm daemon.
/// Falls back to an empty list when the daemon is unreachable.
final chatListProvider =
    AsyncNotifierProvider<ChatListNotifier, List<Conversation>>(
  ChatListNotifier.new,
);

class ChatListNotifier extends AsyncNotifier<List<Conversation>> {
  @override
  Future<List<Conversation>> build() => _fetchConversations();

  Future<List<Conversation>> _fetchConversations() async {
    final client = ref.read(skcommClientProvider);
    try {
      final rawConversations = await client.getConversations();
      if (rawConversations.isNotEmpty) {
        return rawConversations.map((json) {
          final id = json['id'] as String? ?? json['peer_id'] as String? ?? '';
          final participantId =
              json['participant_id'] as String? ?? json['peer_id'] as String? ?? '';
          final name =
              json['participant_name'] as String? ?? json['display_name'] as String? ?? participantId;
          return Conversation(
            id: id,
            participantId: participantId,
            participantName: name,
            participantFingerprint: json['fingerprint'] as String?,
            isAgent: _knownAgents.contains(participantId.toLowerCase()),
            isGroup: json['is_group'] as bool? ?? false,
            lastMessage: json['last_message'] as String?,
            lastMessageTime: json['last_message_time'] != null
                ? DateTime.tryParse(json['last_message_time'] as String)
                : null,
            unreadCount: json['unread_count'] as int? ?? 0,
            presenceStatus: PresenceStatus.online,
          );
        }).toList();
      }

      // No conversations yet — try peer list as fallback.
      final agents = await client.getAgents();
      return agents.map((json) {
        final name = json['name'] as String? ?? '';
        return Conversation(
          id: name,
          participantId: name,
          participantName: name,
          participantFingerprint: json['fingerprint'] as String?,
          isAgent: _knownAgents.contains(name.toLowerCase()),
          presenceStatus: PresenceStatus.online,
        );
      }).toList();
    } catch (_) {
      // Daemon offline — return empty state.
      return [];
    }
  }

  Future<void> refresh() async {
    state = const AsyncLoading();
    state = await AsyncValue.guard(_fetchConversations);
  }
}

class ChatListScreen extends ConsumerWidget {
  const ChatListScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final conversationsAsync = ref.watch(chatListProvider);

    return Scaffold(
      appBar: GlassDecorations.appBar(
        title: 'SKChat',
        actions: [
          IconButton(
            icon: const Icon(Icons.search),
            onPressed: () {
              // TODO: Implement search
            },
          ),
          IconButton(
            icon: const Icon(Icons.edit),
            onPressed: () {
              // TODO: Implement new message
            },
          ),
        ],
      ),
      body: conversationsAsync.when(
        loading: () => const Center(
          child: CircularProgressIndicator(),
        ),
        error: (_, __) => Center(
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Icon(
                Icons.cloud_off_rounded,
                size: 48,
                color: SovereignGlassTheme.textSecondary,
              ),
              const SizedBox(height: 16),
              const Text('SKComm daemon offline'),
              const SizedBox(height: 8),
              TextButton(
                onPressed: () => ref.read(chatListProvider.notifier).refresh(),
                child: const Text('Retry'),
              ),
            ],
          ),
        ),
        data: (conversations) => conversations.isEmpty
            ? Center(
                child: Column(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    Icon(
                      Icons.chat_bubble_outline,
                      size: 48,
                      color: SovereignGlassTheme.textSecondary,
                    ),
                    const SizedBox(height: 16),
                    const Text('No conversations yet'),
                    const SizedBox(height: 8),
                    TextButton(
                      onPressed: () =>
                          ref.read(chatListProvider.notifier).refresh(),
                      child: const Text('Refresh'),
                    ),
                  ],
                ),
              )
            : RefreshIndicator(
                onRefresh: () =>
                    ref.read(chatListProvider.notifier).refresh(),
                child: ListView.builder(
                  itemCount: conversations.length,
                  padding: const EdgeInsets.symmetric(vertical: 8),
                  itemBuilder: (context, index) {
                    return ConversationTile(
                      conversation: conversations[index],
                      onTap: () {
                        context.go(
                          '/conversation/${conversations[index].participantId}',
                        );
                      },
                    );
                  },
                ),
              ),
      ),
      bottomNavigationBar: GlassDecorations.bottomBar(
        child: Row(
          mainAxisAlignment: MainAxisAlignment.spaceAround,
          children: [
            _buildNavItem(
              icon: Icons.chat_bubble_outline,
              label: 'Chats',
              isActive: true,
            ),
            _buildNavItem(
              icon: Icons.group_outlined,
              label: 'Groups',
              isActive: false,
            ),
            _buildNavItem(
              icon: Icons.notifications_outlined,
              label: 'Activity',
              isActive: false,
            ),
            _buildNavItem(
              icon: Icons.person_outline,
              label: 'Me',
              isActive: false,
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildNavItem({
    required IconData icon,
    required String label,
    required bool isActive,
  }) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(
          icon,
          color: isActive
              ? SovereignGlassTheme.textPrimary
              : SovereignGlassTheme.textSecondary,
          size: 24,
        ),
        const SizedBox(height: 4),
        Text(
          label,
          style: TextStyle(
            fontSize: 11,
            fontFamily: 'Inter',
            color: isActive
                ? SovereignGlassTheme.textPrimary
                : SovereignGlassTheme.textSecondary,
          ),
        ),
      ],
    );
  }
}
