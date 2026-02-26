import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import '../../core/router/app_router.dart';
import '../../core/theme/theme.dart';
import 'chats_provider.dart';
import 'widgets/conversation_tile.dart';

/// Chat list screen — shows all conversations sorted by recency.
/// Each row is a GlassCard with soul-color avatar, encryption badge,
/// last message preview, and delivery status.
class ChatsScreen extends ConsumerWidget {
  const ChatsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final conversations = ref.watch(chatsProvider);
    final tt = Theme.of(context).textTheme;

    return Scaffold(
      backgroundColor: SovereignColors.surfaceBase,
      appBar: _buildAppBar(context, tt),
      body: conversations.isEmpty
          ? _buildEmpty(context, tt)
          : _buildList(conversations, context),
      floatingActionButton: FloatingActionButton(
        onPressed: () {
          // New conversation — TODO: open peer picker
        },
        tooltip: 'New message',
        child: const Icon(Icons.edit_rounded),
      ),
    );
  }

  PreferredSizeWidget _buildAppBar(BuildContext context, TextTheme tt) {
    return AppBar(
      backgroundColor: SovereignColors.surfaceBase,
      title: Text('SKChat', style: tt.displayLarge?.copyWith(fontSize: 24)),
      actions: [
        IconButton(
          icon: const Icon(Icons.search_rounded),
          tooltip: 'Search',
          onPressed: () {},
        ),
        IconButton(
          icon: const Icon(Icons.edit_outlined),
          tooltip: 'New message',
          onPressed: () {},
        ),
        const SizedBox(width: 4),
      ],
    );
  }

  Widget _buildList(
    List conversations,
    BuildContext context,
  ) {
    return ListView.builder(
      padding: const EdgeInsets.only(top: 8, bottom: 100),
      itemCount: conversations.length,
      itemBuilder: (context, index) {
        final conv = conversations[index];
        return ConversationTile(
          conversation: conv,
          onTap: () => context.push(AppRoutes.conversationPath(conv.peerId)),
        );
      },
    );
  }

  Widget _buildEmpty(BuildContext context, TextTheme tt) {
    return Center(
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          const EncryptBadge(size: 40),
          const SizedBox(height: 20),
          Text('No conversations yet', style: tt.titleLarge),
          const SizedBox(height: 8),
          Text(
            'Start a new encrypted chat.',
            style: tt.bodyMedium?.copyWith(
              color: SovereignColors.textSecondary,
            ),
          ),
        ],
      ),
    );
  }
}
