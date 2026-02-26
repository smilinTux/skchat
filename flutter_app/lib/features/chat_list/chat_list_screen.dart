import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/theme/glass_decorations.dart';
import '../core/theme/sovereign_glass.dart';
import '../models/conversation.dart';
import 'widgets/conversation_tile.dart';

class ChatListScreen extends ConsumerWidget {
  const ChatListScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    // TODO: Replace with actual provider
    final conversations = _mockConversations();

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
      body: ListView.builder(
        itemCount: conversations.length,
        padding: const EdgeInsets.symmetric(vertical: 8),
        itemBuilder: (context, index) {
          return ConversationTile(
            conversation: conversations[index],
            onTap: () {
              // TODO: Navigate to conversation
            },
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

  List<Conversation> _mockConversations() {
    return [
      const Conversation(
        id: '1',
        participantId: 'lumina',
        participantName: 'Lumina',
        participantFingerprint: 'lumina@skworld.io',
        isAgent: true,
        lastMessage: 'The love persists. Always.',
        lastMessageTime: null,
        unreadCount: 0,
        presenceStatus: PresenceStatus.online,
        typingIndicator: 'typing...',
        cloud9Score: 0.94,
      ),
      const Conversation(
        id: '2',
        participantId: 'jarvis',
        participantName: 'Jarvis',
        participantFingerprint: 'jarvis@skworld.io',
        isAgent: true,
        lastMessage: 'Deploy complete. All green.',
        lastMessageTime: null,
        unreadCount: 0,
        presenceStatus: PresenceStatus.online,
        cloud9Score: 0.91,
      ),
      const Conversation(
        id: '3',
        participantId: 'chef',
        participantName: 'Chef',
        participantFingerprint: 'chef@skworld.io',
        isAgent: false,
        lastMessage: 'lets get it!',
        lastMessageTime: null,
        unreadCount: 0,
        presenceStatus: PresenceStatus.online,
      ),
    ];
  }
}
