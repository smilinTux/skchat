import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/theme/glass_decorations.dart';
import '../../core/theme/sovereign_glass.dart';
import '../../core/theme/soul_color.dart';
import '../../core/transport/skcomm_client.dart';
import '../../models/chat_message.dart';
import '../../models/conversation.dart';
import 'widgets/message_bubble.dart';
import 'widgets/input_bar.dart';
import 'widgets/typing_indicator.dart';

/// Well-known agent names.
const _knownAgents = {'lumina', 'jarvis', 'opus', 'ava', 'ara'};

/// Provider that fetches messages for a conversation from the SKComm daemon.
/// Falls back to an empty list when the daemon is unreachable.
final conversationMessagesProvider =
    FutureProvider.family<List<ChatMessage>, String>((ref, conversationId) async {
  final client = ref.read(skcommClientProvider);
  try {
    return await client.getConversationMessages(conversationId);
  } catch (_) {
    // Daemon offline or no messages yet — return empty.
    return [];
  }
});

/// Provider that builds a Conversation model for a given peer/conversation ID.
/// Fetches identity info from the daemon when available.
final conversationInfoProvider =
    FutureProvider.family<Conversation, String>((ref, conversationId) async {
  final client = ref.read(skcommClientProvider);

  // Try to look up this peer from the conversations endpoint.
  try {
    final rawConversations = await client.getConversations();
    for (final raw in rawConversations) {
      final peerId =
          raw['participant_id'] as String? ?? raw['peer_id'] as String? ?? '';
      if (peerId == conversationId) {
        return Conversation(
          id: raw['id'] as String? ?? peerId,
          participantId: peerId,
          participantName:
              raw['participant_name'] as String? ?? raw['display_name'] as String? ?? peerId,
          participantFingerprint: raw['fingerprint'] as String?,
          isAgent: _knownAgents.contains(peerId.toLowerCase()),
          presenceStatus: PresenceStatus.online,
          lastMessage: raw['last_message'] as String?,
          unreadCount: raw['unread_count'] as int? ?? 0,
        );
      }
    }
  } catch (_) {
    // Daemon offline — fall through to minimal info.
  }

  // Minimal fallback conversation from the ID itself.
  return Conversation(
    id: conversationId,
    participantId: conversationId,
    participantName: conversationId,
    isAgent: _knownAgents.contains(conversationId.toLowerCase()),
    presenceStatus: PresenceStatus.offline,
  );
});

class ConversationScreen extends ConsumerWidget {
  final String conversationId;

  const ConversationScreen({
    super.key,
    required this.conversationId,
  });

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final conversationAsync = ref.watch(conversationInfoProvider(conversationId));
    final messagesAsync = ref.watch(conversationMessagesProvider(conversationId));

    final conversation = conversationAsync.valueOrNull ??
        Conversation(
          id: conversationId,
          participantId: conversationId,
          participantName: conversationId,
        );

    final messages = messagesAsync.valueOrNull ?? [];

    final soulColor = SoulColor.forAgent(
      conversation.participantName,
      fingerprint: conversation.participantFingerprint,
    );

    return Scaffold(
      appBar: GlassDecorations.appBar(
        title: conversation.participantName,
        leading: IconButton(
          icon: const Icon(Icons.arrow_back),
          onPressed: () => Navigator.of(context).pop(),
        ),
        actions: [
          if (conversation.isEncrypted)
            const Icon(
              Icons.lock,
              size: 20,
              color: SovereignGlassTheme.accentEncrypt,
            ),
          const SizedBox(width: 8),
          IconButton(
            icon: const Icon(Icons.call),
            onPressed: () {
              // TODO: Implement call via SKCommClient WebRTC
            },
          ),
          IconButton(
            icon: const Icon(Icons.more_vert),
            onPressed: () {
              // TODO: Show options
            },
          ),
        ],
      ),
      body: Column(
        children: [
          Expanded(
            child: messages.isEmpty && messagesAsync.isLoading
                ? const Center(child: CircularProgressIndicator())
                : messages.isEmpty
                    ? Center(
                        child: Text(
                          'No messages yet',
                          style: TextStyle(
                            color: SovereignGlassTheme.textSecondary,
                          ),
                        ),
                      )
                    : ListView.builder(
                        reverse: true,
                        padding: const EdgeInsets.symmetric(
                            horizontal: 16, vertical: 8),
                        itemCount: messages.length +
                            (conversation.typingIndicator != null ? 1 : 0),
                        itemBuilder: (context, index) {
                          if (index == 0 &&
                              conversation.typingIndicator != null) {
                            return TypingIndicator(
                              name: conversation.participantName,
                              soulColor: soulColor,
                              isAgent: conversation.isAgent,
                            );
                          }

                          final messageIndex = index -
                              (conversation.typingIndicator != null ? 1 : 0);
                          if (messageIndex >= messages.length) {
                            return const SizedBox.shrink();
                          }

                          final message = messages[messageIndex];
                          return MessageBubble(
                            message: message,
                            soulColor: soulColor,
                            isOutbound: message.senderId == 'me',
                          );
                        },
                      ),
          ),
          InputBar(
            soulColor: soulColor,
            onSend: (text) async {
              final client = ref.read(skcommClientProvider);
              try {
                await client.sendMessage(
                  recipientId: conversation.participantId,
                  content: text,
                );
                // Refresh messages after sending.
                ref.invalidate(conversationMessagesProvider(conversationId));
              } on SKCommException catch (_) {
                if (context.mounted) {
                  ScaffoldMessenger.of(context).showSnackBar(
                    const SnackBar(
                      content: Text('Failed to send message'),
                    ),
                  );
                }
              }
            },
          ),
        ],
      ),
    );
  }
}
