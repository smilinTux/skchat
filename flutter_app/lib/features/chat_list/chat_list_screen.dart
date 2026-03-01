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

class ChatListScreen extends ConsumerStatefulWidget {
  const ChatListScreen({super.key});

  @override
  ConsumerState<ChatListScreen> createState() => _ChatListScreenState();
}

class _ChatListScreenState extends ConsumerState<ChatListScreen> {
  bool _isSearching = false;
  final _searchController = TextEditingController();
  String _searchQuery = '';

  @override
  void dispose() {
    _searchController.dispose();
    super.dispose();
  }

  /// Filter conversations by agent name matching the search query.
  List<Conversation> _filterConversations(List<Conversation> conversations) {
    if (_searchQuery.isEmpty) return conversations;
    final query = _searchQuery.toLowerCase();
    return conversations.where((c) {
      return c.participantName.toLowerCase().contains(query) ||
          c.participantId.toLowerCase().contains(query);
    }).toList();
  }

  /// Show a dialog to pick an agent for a new conversation.
  Future<void> _showNewMessageDialog(BuildContext context) async {
    final client = ref.read(skcommClientProvider);
    List<Map<String, dynamic>> agents = [];
    try {
      agents = await client.getAgents();
    } catch (_) {
      // Daemon offline -- show manual entry instead.
    }

    if (!context.mounted) return;

    final recipientController = TextEditingController();

    final selected = await showDialog<String>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        backgroundColor: const Color(0xFF1A1A2E),
        title: const Text('New Message'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              controller: recipientController,
              style: const TextStyle(color: SovereignGlassTheme.textPrimary),
              decoration: const InputDecoration(
                hintText: 'Agent name...',
                hintStyle: TextStyle(color: SovereignGlassTheme.textSecondary),
                prefixIcon: Icon(Icons.person_search,
                    color: SovereignGlassTheme.textSecondary),
              ),
            ),
            if (agents.isNotEmpty) ...[
              const SizedBox(height: 16),
              const Align(
                alignment: Alignment.centerLeft,
                child: Text(
                  'Known agents',
                  style: TextStyle(
                    color: SovereignGlassTheme.textSecondary,
                    fontSize: 12,
                  ),
                ),
              ),
              const SizedBox(height: 8),
              ConstrainedBox(
                constraints: const BoxConstraints(maxHeight: 200),
                child: ListView.builder(
                  shrinkWrap: true,
                  itemCount: agents.length,
                  itemBuilder: (context, index) {
                    final name = agents[index]['name'] as String? ?? '';
                    return ListTile(
                      dense: true,
                      leading: Icon(
                        _knownAgents.contains(name.toLowerCase())
                            ? Icons.smart_toy
                            : Icons.person,
                        color: SovereignGlassTheme.textSecondary,
                        size: 20,
                      ),
                      title: Text(
                        name,
                        style: const TextStyle(
                          color: SovereignGlassTheme.textPrimary,
                        ),
                      ),
                      onTap: () => Navigator.of(dialogContext).pop(name),
                    );
                  },
                ),
              ),
            ],
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dialogContext).pop(null),
            child: const Text('Cancel'),
          ),
          TextButton(
            onPressed: () {
              final text = recipientController.text.trim();
              if (text.isNotEmpty) {
                Navigator.of(dialogContext).pop(text);
              }
            },
            child: const Text('Start Chat'),
          ),
        ],
      ),
    );

    recipientController.dispose();

    if (selected != null && selected.isNotEmpty && context.mounted) {
      context.go('/conversation/$selected');
    }
  }

  @override
  Widget build(BuildContext context) {
    final conversationsAsync = ref.watch(chatListProvider);

    return Scaffold(
      appBar: _isSearching
          ? GlassDecorations.appBar(
              title: '',
              titleWidget: TextField(
                controller: _searchController,
                autofocus: true,
                style: const TextStyle(
                  color: SovereignGlassTheme.textPrimary,
                  fontSize: 16,
                ),
                decoration: const InputDecoration(
                  hintText: 'Search agents...',
                  hintStyle: TextStyle(
                    color: SovereignGlassTheme.textSecondary,
                  ),
                  border: InputBorder.none,
                ),
                onChanged: (value) {
                  setState(() {
                    _searchQuery = value;
                  });
                },
              ),
              leading: IconButton(
                icon: const Icon(Icons.arrow_back),
                onPressed: () {
                  setState(() {
                    _isSearching = false;
                    _searchQuery = '';
                    _searchController.clear();
                  });
                },
              ),
              actions: [
                if (_searchController.text.isNotEmpty)
                  IconButton(
                    icon: const Icon(Icons.clear),
                    onPressed: () {
                      setState(() {
                        _searchQuery = '';
                        _searchController.clear();
                      });
                    },
                  ),
              ],
            )
          : GlassDecorations.appBar(
              title: 'SKChat',
              actions: [
                IconButton(
                  icon: const Icon(Icons.search),
                  onPressed: () {
                    setState(() {
                      _isSearching = true;
                    });
                  },
                ),
                IconButton(
                  icon: const Icon(Icons.edit),
                  onPressed: () => _showNewMessageDialog(context),
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
        data: (conversations) {
          final filtered = _filterConversations(conversations);
          if (filtered.isEmpty && conversations.isEmpty) {
            return Center(
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
            );
          }
          if (filtered.isEmpty && _searchQuery.isNotEmpty) {
            return Center(
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  Icon(
                    Icons.search_off,
                    size: 48,
                    color: SovereignGlassTheme.textSecondary,
                  ),
                  const SizedBox(height: 16),
                  Text(
                    'No results for "$_searchQuery"',
                    style: const TextStyle(
                      color: SovereignGlassTheme.textSecondary,
                    ),
                  ),
                ],
              ),
            );
          }
          return RefreshIndicator(
            onRefresh: () => ref.read(chatListProvider.notifier).refresh(),
            child: ListView.builder(
              itemCount: filtered.length,
              padding: const EdgeInsets.symmetric(vertical: 8),
              itemBuilder: (context, index) {
                return ConversationTile(
                  conversation: filtered[index],
                  onTap: () {
                    context.go(
                      '/conversation/${filtered[index].participantId}',
                    );
                  },
                );
              },
            ),
          );
        },
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
