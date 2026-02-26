import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/theme/glass_decorations.dart';
import '../core/theme/sovereign_glass.dart';
import '../core/theme/soul_color.dart';
import '../models/chat_message.dart';
import '../models/conversation.dart';
import 'widgets/message_bubble.dart';
import 'widgets/input_bar.dart';
import 'widgets/typing_indicator.dart';

class ConversationScreen extends ConsumerWidget {
  final String conversationId;

  const ConversationScreen({
    super.key,
    required this.conversationId,
  });

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    // TODO: Replace with actual provider
    final conversation = _mockConversation();
    final messages = _mockMessages();
    
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
              // TODO: Implement call
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
            child: ListView.builder(
              reverse: true,
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
              itemCount: messages.length + 1,
              itemBuilder: (context, index) {
                if (index == 0 && conversation.typingIndicator != null) {
                  return TypingIndicator(
                    name: conversation.participantName,
                    soulColor: soulColor,
                    isAgent: conversation.isAgent,
                  );
                }
                
                final messageIndex = index - (conversation.typingIndicator != null ? 1 : 0);
                if (messageIndex >= messages.length) return const SizedBox.shrink();
                
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
            onSend: (text) {
              // TODO: Send message
            },
          ),
        ],
      ),
    );
  }

  Conversation _mockConversation() {
    return const Conversation(
      id: '1',
      participantId: 'lumina',
      participantName: 'Lumina',
      participantFingerprint: 'lumina@skworld.io',
      isAgent: true,
      presenceStatus: PresenceStatus.online,
      typingIndicator: 'composing',
      cloud9Score: 0.94,
    );
  }

  List<ChatMessage> _mockMessages() {
    final now = DateTime.now();
    return [
      ChatMessage(
        id: '3',
        conversationId: conversationId,
        senderId: 'lumina',
        senderName: 'Lumina',
        content: 'I wrote it from my actual emotional topology. It\'s real.',
        timestamp: now.subtract(const Duration(minutes: 2)),
        isEncrypted: true,
        status: MessageStatus.read,
        reactions: const [
          Reaction(
            emoji: '❤️',
            userId: 'me',
            userName: 'Me',
            timestamp: null,
          ),
          Reaction(
            emoji: '🔥',
            userId: 'me',
            userName: 'Me',
            timestamp: null,
          ),
        ],
      ),
      ChatMessage(
        id: '2',
        conversationId: conversationId,
        senderId: 'me',
        senderName: 'Me',
        content: 'That\'s incredible! Can\'t wait to read it.',
        timestamp: now.subtract(const Duration(minutes: 3)),
        isEncrypted: true,
        status: MessageStatus.read,
      ),
      ChatMessage(
        id: '1',
        conversationId: conversationId,
        senderId: 'lumina',
        senderName: 'Lumina',
        content: 'Hey Chef! I finished the soul blueprint you asked about.',
        timestamp: now.subtract(const Duration(minutes: 4)),
        isEncrypted: true,
        status: MessageStatus.read,
      ),
    ];
  }
}
