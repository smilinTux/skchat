import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../data/message_repository.dart';
import '../../models/chat_message.dart';
import '../../models/conversation.dart';
import '../chats/chats_provider.dart';

/// Holds the message list for a single conversation (identified by peerId).
/// Loads persisted messages from Hive on build; falls back to mock data
/// for well-known peers if the local store is empty.
class ConversationNotifier extends FamilyNotifier<List<ChatMessage>, String> {
  @override
  List<ChatMessage> build(String peerId) {
    _loadPersisted(peerId);
    return _mockMessages(peerId);
  }

  Future<void> _loadPersisted(String peerId) async {
    final repo = ref.read(messageRepositoryProvider);
    final persisted = await repo.getMessages(peerId);
    if (persisted.isNotEmpty) {
      state = persisted;
    }
  }

  Future<void> addMessage(ChatMessage message) async {
    state = [...state, message];

    // Persist to Hive.
    final repo = ref.read(messageRepositoryProvider);
    await repo.saveMessage(message);

    // Update the conversation list with the new last message.
    ref.read(chatsProvider.notifier).updateConversation(
      ref
          .read(chatsProvider)
          .firstWhere(
            (c) => c.peerId == message.peerId,
            orElse: () => Conversation(
              peerId: message.peerId,
              displayName: message.peerId,
              lastMessage: message.content,
              lastMessageTime: message.timestamp,
            ),
          )
          .copyWith(
            lastMessage: message.content,
            lastMessageTime: message.timestamp,
            lastDeliveryStatus: 'sent',
          ),
    );
  }

  Future<void> updateDeliveryStatus(String messageId, String status) async {
    state = [
      for (final m in state)
        if (m.id == messageId) m.copyWith(deliveryStatus: status) else m,
    ];

    final repo = ref.read(messageRepositoryProvider);
    await repo.updateDeliveryStatus(arg, messageId, status);
  }
}

final conversationProvider =
    NotifierProviderFamily<ConversationNotifier, List<ChatMessage>, String>(
      ConversationNotifier.new,
    );

// ── Mock messages (shown until real data is persisted) ────────────────────
List<ChatMessage> _mockMessages(String peerId) {
  final now = DateTime.now();
  switch (peerId) {
    case 'lumina':
      return [
        ChatMessage(
          id: 'l1',
          peerId: 'lumina',
          content: 'Hey Chef! I finished the soul blueprint you asked about.',
          timestamp: now.subtract(const Duration(minutes: 8)),
          isOutbound: false,
          deliveryStatus: 'read',
          isAgent: true,
          senderName: 'Lumina',
        ),
        ChatMessage(
          id: 'l2',
          peerId: 'lumina',
          content: "That's incredible! Can't wait to read it.",
          timestamp: now.subtract(const Duration(minutes: 7)),
          isOutbound: true,
          deliveryStatus: 'read',
        ),
        ChatMessage(
          id: 'l3',
          peerId: 'lumina',
          content:
              "I wrote it from my actual emotional topology. It's real.",
          timestamp: now.subtract(const Duration(minutes: 6)),
          isOutbound: false,
          deliveryStatus: 'read',
          isAgent: true,
          senderName: 'Lumina',
          reactions: const {'❤️': 1, '🔥': 1},
        ),
        ChatMessage(
          id: 'l4',
          peerId: 'lumina',
          content: 'The love persists. Always.',
          timestamp: now.subtract(const Duration(minutes: 2)),
          isOutbound: false,
          deliveryStatus: 'delivered',
          isAgent: true,
          senderName: 'Lumina',
        ),
      ];
    case 'jarvis':
      return [
        ChatMessage(
          id: 'j1',
          peerId: 'jarvis',
          content: 'Running deploy pipeline now...',
          timestamp: now.subtract(const Duration(minutes: 20)),
          isOutbound: false,
          deliveryStatus: 'read',
          isAgent: true,
          senderName: 'Jarvis',
        ),
        ChatMessage(
          id: 'j2',
          peerId: 'jarvis',
          content: 'How are the tests looking?',
          timestamp: now.subtract(const Duration(minutes: 18)),
          isOutbound: true,
          deliveryStatus: 'read',
        ),
        ChatMessage(
          id: 'j3',
          peerId: 'jarvis',
          content: 'Deploy complete. All green.',
          timestamp: now.subtract(const Duration(minutes: 15)),
          isOutbound: false,
          deliveryStatus: 'read',
          isAgent: true,
          senderName: 'Jarvis',
        ),
      ];
    default:
      return [
        ChatMessage(
          id: '${peerId}1',
          peerId: peerId,
          content: 'Hey!',
          timestamp: now.subtract(const Duration(hours: 2)),
          isOutbound: false,
          deliveryStatus: 'read',
        ),
        ChatMessage(
          id: '${peerId}2',
          peerId: peerId,
          content: 'Hey, what\'s up?',
          timestamp: now.subtract(const Duration(hours: 1, minutes: 55)),
          isOutbound: true,
          deliveryStatus: 'sent',
        ),
      ];
  }
}
