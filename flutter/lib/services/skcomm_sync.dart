import 'dart:async';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../core/theme/sovereign_colors.dart';
import '../features/calls/call_provider.dart';
import '../models/call_state.dart';
import '../models/chat_message.dart';
import '../models/conversation.dart';
import '../features/chats/chats_provider.dart';
import '../features/conversation/conversation_provider.dart';
import 'daemon_service.dart';
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
  // CLI polling is slightly slower to reduce subprocess overhead.
  static const _cliPollInterval = Duration(seconds: 10);

  Timer? _pollTimer;
  Timer? _daemonTimer;
  Timer? _cliPollTimer;
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
    _pollSkchatCli();

    _pollTimer = Timer.periodic(_pollInterval, (_) => _pollInbox());
    _daemonTimer = Timer.periodic(_daemonCheckInterval, (_) => _checkDaemon());
    _cliPollTimer = Timer.periodic(_cliPollInterval, (_) => _pollSkchatCli());
  }

  void _stopPolling() {
    _pollTimer?.cancel();
    _daemonTimer?.cancel();
    _cliPollTimer?.cancel();
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

  /// Poll the skchat local history store via CLI and dispatch any new messages.
  ///
  /// Runs `skchat inbox --json --limit 100` from $HOME and dispatches messages
  /// not yet seen by the HTTP poller.  Outbound detection uses $SKCHAT_IDENTITY.
  Future<void> _pollSkchatCli() async {
    final daemon = ref.read(daemonServiceProvider);
    try {
      final messages = await daemon.getInbox(limit: 100);
      final localId = daemon.localIdentity;
      final localShort =
          localId != null ? DaemonService.peerShortName(localId).toLowerCase() : null;

      for (final msg in messages) {
        if (_seenEnvelopeIds.contains(msg.id)) continue;
        _seenEnvelopeIds.add(msg.id);

        final senderShort =
            DaemonService.peerShortName(msg.sender).toLowerCase();
        final recipientShort =
            DaemonService.peerShortName(msg.recipient).toLowerCase();
        final isOutbound = localShort != null && senderShort == localShort;

        // peerId is the *other* party in the conversation.
        final peerId = isOutbound ? recipientShort : senderShort;

        _dispatchCliMessage(
          id: msg.id,
          peerId: peerId,
          content: msg.content,
          timestamp: msg.timestamp,
          isOutbound: isOutbound,
        );
      }
    } catch (_) {
      // CLI unavailable — silently ignore.
    }
  }

  /// Send a message via the skchat CLI (primary), falling back to HTTP.
  ///
  /// The CLI path stores the message locally AND delivers via SKComm transport.
  /// Returns the envelope ID (from HTTP) or a CLI-generated token on success,
  /// null on complete failure.
  Future<String?> sendMessage({
    required String peerId,
    required String content,
    String? threadId,
    String? inReplyTo,
  }) async {
    // Primary: skchat CLI — local store + transport delivery.
    final daemon = ref.read(daemonServiceProvider);
    final cliResult = await daemon.sendMessage(
      recipient: peerId,
      content: content,
    );
    if (cliResult.success) {
      // Return a synthetic ID so callers can track the message.
      return 'cli_${DateTime.now().millisecondsSinceEpoch}';
    }

    // Fallback: SKComm HTTP (transport only, no local store).
    final client = ref.read(skcommClientProvider);
    try {
      final result = await client.sendMessage(
        recipient: peerId,
        message: content,
        threadId: threadId,
        inReplyTo: inReplyTo,
      );
      return result.delivered ? result.envelopeId : null;
    } catch (_) {
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
    // Intercept call-request sentinel before showing in chat.
    if (msg.content.startsWith('__CALL_REQUEST__:')) {
      _handleIncomingCallRequest(msg);
      return;
    }

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

  /// Dispatch a message sourced from the skchat CLI into Riverpod state.
  ///
  /// Unlike [_dispatchIncoming] which always marks isOutbound=false,
  /// this helper handles both directions based on the CLI isOutbound flag.
  void _dispatchCliMessage({
    required String id,
    required String peerId,
    required String content,
    required DateTime timestamp,
    required bool isOutbound,
  }) {
    // Skip call sentinels (handled by HTTP path).
    if (content.startsWith('__CALL_REQUEST__:')) return;

    final chatMsg = ChatMessage(
      id: id,
      peerId: peerId,
      content: content,
      timestamp: timestamp,
      isOutbound: isOutbound,
      deliveryStatus: isOutbound ? 'sent' : 'delivered',
    );

    ref.read(conversationProvider(peerId).notifier).addMessage(chatMsg);

    final chats = ref.read(chatsProvider);
    final exists = chats.any((c) => c.peerId == peerId);
    if (exists) {
      ref.read(chatsProvider.notifier).updateConversation(
        chats
            .firstWhere((c) => c.peerId == peerId)
            .copyWith(
              lastMessage: content,
              lastMessageTime: timestamp,
              lastDeliveryStatus: isOutbound ? 'sent' : 'delivered',
            ),
      );
    } else if (!isOutbound) {
      // Only auto-create conversation entries for inbound messages.
      ref.read(chatsProvider.notifier).addConversation(
        Conversation(
          peerId: peerId,
          displayName: peerId,
          lastMessage: content,
          lastMessageTime: timestamp,
          soulFingerprint: peerId,
          lastDeliveryStatus: 'delivered',
          unreadCount: 1,
        ),
      );
    }
  }

  void _handleIncomingCallRequest(InboxMessage msg) {
    // Derive call type from sentinel suffix: __CALL_REQUEST__:video or :voice
    final suffix = msg.content.split(':').elementAtOrNull(1) ?? 'voice';
    final callType = suffix == 'video' ? CallType.video : CallType.voice;

    // Look up conversation for display name and soul color.
    final chats = ref.read(chatsProvider);
    final conv = chats.cast<Conversation?>().firstWhere(
          (c) => c?.peerId == msg.sender,
          orElse: () => null,
        );

    ref.read(callProvider.notifier).incomingCall(
      peerId: msg.sender,
      peerName: conv?.displayName ?? msg.sender,
      peerSoulColor: conv?.resolvedSoulColor ??
          SovereignColors.fromFingerprint(msg.sender),
      type: callType,
    );
  }
}

final skcommSyncProvider =
    NotifierProvider<SKCommSyncNotifier, DaemonState>(SKCommSyncNotifier.new);
