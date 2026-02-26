import 'dart:async';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../models/chat_message.dart';
import '../models/conversation.dart';
import '../features/chats/chats_provider.dart';
import '../features/conversation/conversation_provider.dart';
import 'skcomm_client.dart';

/// Daemon connection status.
enum DaemonStatus { connecting, online, offline, error }

class DaemonState {
  const DaemonState({
    this.status = DaemonStatus.connecting,
    this.errorMessage,
    this.lastPollAt,
    this.transportInfo,
  });

  final DaemonStatus status;
  final String? errorMessage;
  final DateTime? lastPollAt;
  final Map<String, dynamic>? transportInfo;

  DaemonState copyWith({
    DaemonStatus? status,
    String? errorMessage,
    DateTime? lastPollAt,
    Map<String, dynamic>? transportInfo,
  }) {
    return DaemonState(
      status: status ?? this.status,
      errorMessage: errorMessage,
      lastPollAt: lastPollAt ?? this.lastPollAt,
      transportInfo: transportInfo ?? this.transportInfo,
    );
  }
}

/// Manages polling the SKComm daemon and syncing messages into Riverpod state.
/// Polls every [pollInterval] (default 5s for foreground, 30s for background).
class SKCommSyncNotifier extends Notifier<DaemonState> {
  static const _pollInterval = Duration(seconds: 5);
  static const _daemonCheckInterval = Duration(seconds: 15);

  Timer? _pollTimer;
  Timer? _daemonTimer;
  final Set<String> _seenEnvelopeIds = {};

  @override
  DaemonState build() {
    // Defer polling so Riverpod state is fully initialized before first read.
    Future.microtask(_startPolling);
    ref.onDispose(_stopPolling);
    return const DaemonState();
  }

  void _startPolling() {
    _checkDaemon();
    _pollInbox();

    _pollTimer = Timer.periodic(_pollInterval, (_) => _pollInbox());
    _daemonTimer = Timer.periodic(_daemonCheckInterval, (_) => _checkDaemon());
  }

  void _stopPolling() {
    _pollTimer?.cancel();
    _daemonTimer?.cancel();
  }

  /// Check daemon health and update connection status.
  Future<void> _checkDaemon() async {
    final client = ref.read(skcommClientProvider);
    try {
      final alive = await client.isAlive();
      if (alive) {
        final statusInfo = await client.getStatus();
        state = state.copyWith(
          status: DaemonStatus.online,
          errorMessage: null,
          lastPollAt: DateTime.now(),
          transportInfo: statusInfo,
        );
      } else {
        state = state.copyWith(status: DaemonStatus.offline);
      }
    } catch (e) {
      state = state.copyWith(
        status: DaemonStatus.offline,
        errorMessage: e.toString(),
      );
    }
  }

  /// Poll inbox and dispatch new messages to conversation providers.
  Future<void> _pollInbox() async {
    if (state.status == DaemonStatus.offline) return;

    final client = ref.read(skcommClientProvider);
    try {
      final messages = await client.getInbox();
      for (final msg in messages) {
        if (_seenEnvelopeIds.contains(msg.envelopeId)) continue;
        _seenEnvelopeIds.add(msg.envelopeId);
        _dispatchIncoming(msg);
      }
      state = state.copyWith(
        status: DaemonStatus.online,
        lastPollAt: DateTime.now(),
      );
    } catch (e) {
      // Don't flip status to offline on a single poll failure.
    }
  }

  /// Send a message via the daemon.
  /// Returns the envelope ID on success, null on failure.
  Future<String?> sendMessage({
    required String peerId,
    required String content,
    String? threadId,
    String? inReplyTo,
  }) async {
    final client = ref.read(skcommClientProvider);
    try {
      final result = await client.sendMessage(
        recipient: peerId,
        message: content,
        threadId: threadId,
        inReplyTo: inReplyTo,
      );
      return result.delivered ? result.envelopeId : null;
    } catch (e) {
      return null;
    }
  }

  /// Broadcast presence online.
  Future<void> broadcastOnline() async {
    final client = ref.read(skcommClientProvider);
    try {
      await client.updatePresence(status: 'online');
    } catch (_) {}
  }

  // ── Routing incoming messages into state ──────────────────────────────────

  void _dispatchIncoming(InboxMessage msg) {
    final chatMsg = ChatMessage(
      id: msg.envelopeId,
      peerId: msg.sender,
      content: msg.content,
      timestamp: msg.createdAt,
      isOutbound: false,
      deliveryStatus: 'delivered',
      isEncrypted: msg.isEncrypted,
      replyToId: msg.inReplyTo,
    );

    // Add to the conversation message list.
    ref.read(conversationProvider(msg.sender).notifier).addMessage(chatMsg);

    // Update or create the conversation in the chat list.
    final chats = ref.read(chatsProvider);
    final exists = chats.any((c) => c.peerId == msg.sender);
    if (exists) {
      ref.read(chatsProvider.notifier).updateConversation(
        chats
            .firstWhere((c) => c.peerId == msg.sender)
            .copyWith(
              lastMessage: msg.content,
              lastMessageTime: msg.createdAt,
              lastDeliveryStatus: 'delivered',
              unreadCount: chats
                      .firstWhere((c) => c.peerId == msg.sender)
                      .unreadCount +
                  1,
            ),
      );
    } else {
      // New peer — insert into chat list with derived soul color.
      ref.read(chatsProvider.notifier).addConversation(
        Conversation(
          peerId: msg.sender,
          displayName: msg.sender,
          lastMessage: msg.content,
          lastMessageTime: msg.createdAt,
          soulFingerprint: msg.sender,
          lastDeliveryStatus: 'delivered',
          unreadCount: 1,
        ),
      );
    }
  }
}

final skcommSyncProvider =
    NotifierProvider<SKCommSyncNotifier, DaemonState>(SKCommSyncNotifier.new);
