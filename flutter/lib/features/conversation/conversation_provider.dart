import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../data/message_repository.dart';
import '../../models/chat_message.dart';
import '../../models/conversation.dart';
import '../chats/chats_provider.dart';

/// Holds the message list for a single conversation (identified by peerId).
/// Loads persisted messages from Hive on build.
class ConversationNotifier extends FamilyNotifier<List<ChatMessage>, String> {
  @override
  List<ChatMessage> build(String peerId) {
    _loadPersisted(peerId);
    return [];
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
    await repo.updateDeliveryStatus(this.arg, messageId, status);
  }
}

final conversationProvider =
    NotifierProviderFamily<ConversationNotifier, List<ChatMessage>, String>(
      ConversationNotifier.new,
    );
