import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../data/message_repository.dart';
import '../../models/chat_message.dart';
import '../../models/conversation.dart';
import '../../services/daemon_service.dart';
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

  /// Fetch conversation history from the skchat local store via CLI.
  ///
  /// Calls `skchat inbox --json` and filters messages by [peerId].
  /// Falls back to the SKComm HTTP API for conversation IDs when the CLI
  /// is unavailable.  Merges into Hive-persisted state without duplicates.
  Future<void> _fetchFromDaemon(String peerId) async {
    final daemon = ref.read(daemonServiceProvider);
    final repo = ref.read(messageRepositoryProvider);

    // Primary: skchat CLI conversation history.
    try {
      final cliMessages = await daemon.getConversation(peerId, limit: 100);
      if (cliMessages.isNotEmpty) {
        final localId = daemon.localIdentity;
        final localShort = localId != null
            ? DaemonService.peerShortName(localId).toLowerCase()
            : null;
        final peerShort = DaemonService.peerShortName(peerId).toLowerCase();

        final existing = state.map((m) => m.id).toSet();
        final fresh = <ChatMessage>[];

        for (final m in cliMessages) {
          if (existing.contains(m.id)) continue;
          final senderShort =
              DaemonService.peerShortName(m.sender).toLowerCase();
          final isOutbound =
              localShort != null && senderShort == localShort;
          final msgPeerId = isOutbound
              ? DaemonService.peerShortName(m.recipient).toLowerCase()
              : senderShort;
          // Only include messages that belong to this conversation.
          if (msgPeerId != peerShort) continue;

          fresh.add(ChatMessage(
            id: m.id,
            peerId: msgPeerId,
            content: m.content,
            timestamp: m.timestamp,
            isOutbound: isOutbound,
            deliveryStatus: isOutbound ? 'sent' : 'delivered',
          ));
        }

        if (fresh.isNotEmpty) {
          final merged = [...state, ...fresh];
          merged.sort((a, b) => a.timestamp.compareTo(b.timestamp));
          state = merged;
          for (final msg in fresh) {
            await repo.saveMessage(msg);
          }
        }
        return;
      }
    } catch (_) {
      // CLI unavailable — fall through to HTTP fallback.
    }

    // Fallback: SKComm HTTP conversation listing (no per-message history yet).
    final client = ref.read(skcommClientProvider);
    try {
      final alive = await client.isAlive();
      if (!alive) return;
      // HTTP API does not yet expose per-conversation message history;
      // the CLI path above is the canonical source.  Nothing more to do.
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
