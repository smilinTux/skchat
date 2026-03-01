import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../data/message_repository.dart';
import '../../models/chat_message.dart';
import '../../models/conversation.dart';
import '../../services/skcomm_client.dart';
import '../chats/chats_provider.dart';

/// Holds the message list for a single conversation (identified by peerId).
/// Loads persisted messages from Hive first, then tries to fetch from the
/// SKComm daemon for any new messages not yet persisted.
class ConversationNotifier extends FamilyNotifier<List<ChatMessage>, String> {
  @override
  List<ChatMessage> build(String peerId) {
    _loadPersistedThenDaemon(peerId);
    return [];
  }

  Future<void> _loadPersistedThenDaemon(String peerId) async {
    final repo = ref.read(messageRepositoryProvider);

    // Instant load from Hive.
    final persisted = await repo.getMessages(peerId);
    if (persisted.isNotEmpty) {
      state = persisted;
    }

    // Then try the daemon for fresh data.
    await _fetchFromDaemon(peerId);
  }

  /// Fetch conversation history from the daemon and merge into local state.
  Future<void> _fetchFromDaemon(String peerId) async {
    final client = ref.read(skcommClientProvider);
    final repo = ref.read(messageRepositoryProvider);
    try {
      final alive = await client.isAlive();
      if (!alive) return;

      final conversations = await client.getConversations();
      // Look for a conversation matching this peerId.
      String? conversationId;
      for (final raw in conversations) {
        final pid = raw['peer_id'] as String? ?? '';
        if (pid == peerId) {
          conversationId = raw['id'] as String? ?? pid;
          break;
        }
      }
      if (conversationId == null) return;

      // TODO: SKComm daemon needs GET /api/v1/conversation/:id endpoint
      // to fetch message history for a specific conversation.
      // For now the sync layer (_pollInbox in skcomm_sync.dart) handles
      // real-time message dispatch. This method will be fully wired once
      // the daemon exposes per-conversation message history.
    } catch (_) {
      // Daemon offline — keep Hive data.
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
