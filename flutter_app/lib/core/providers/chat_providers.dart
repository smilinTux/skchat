import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../models/chat_message.dart';
import '../../models/conversation.dart';
import '../transport/skcomm_client.dart';

// ---------------------------------------------------------------------------
// conversationsProvider
// Polls GET /api/v1/messages/inbox every 5 s, groups messages by sender,
// and returns a sorted List<Conversation> wrapped in AsyncValue so screens
// can use .when() for loading / error states.
// ---------------------------------------------------------------------------

class ConversationNotifier
    extends StateNotifier<AsyncValue<List<Conversation>>> {
  ConversationNotifier(this._client) : super(const AsyncLoading()) {
    _fetch();
    _timer = Timer.periodic(const Duration(seconds: 5), (_) => _fetch());
  }

  final SKCommClient _client;
  Timer? _timer;

  Future<void> _fetch() async {
    try {
      final messages = await _client.pollInbox();
      if (!mounted) return;

      // Group messages by senderId (= thread key).
      final grouped = <String, List<ChatMessage>>{};
      for (final msg in messages) {
        grouped.putIfAbsent(msg.senderId, () => []).add(msg);
      }

      // Build a Conversation per thread, sorted newest-first.
      final conversations = grouped.entries.map((entry) {
        final sorted = entry.value
          ..sort((a, b) => b.timestamp.compareTo(a.timestamp));
        final latest = sorted.first;
        return Conversation(
          id: entry.key,
          participantId: entry.key,
          participantName: latest.senderName,
          lastMessage: latest.content,
          lastMessageTime: latest.timestamp,
          unreadCount: sorted.length,
        );
      }).toList()
        ..sort((a, b) =>
            (b.lastMessageTime ?? DateTime.fromMillisecondsSinceEpoch(0))
                .compareTo(
                    a.lastMessageTime ?? DateTime.fromMillisecondsSinceEpoch(0),
                ));

      state = AsyncData(conversations);
    } on Object catch (e, st) {
      if (!mounted) return;
      // On a periodic refresh keep previous data; only set error on first load.
      if (state is AsyncLoading) {
        state = AsyncError(e, st);
      }
    }
  }

  /// Trigger a manual refresh (e.g. pull-to-refresh).
  Future<void> refresh() => _fetch();

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }
}

/// Polls the daemon inbox every 5 s and exposes conversations grouped by
/// sender thread.
///
/// State is `AsyncValue<List<Conversation>>` so widgets can use `.when()`.
final conversationsProvider = StateNotifierProvider<ConversationNotifier,
    AsyncValue<List<Conversation>>>(
  (ref) => ConversationNotifier(ref.read(skcommClientProvider)),
);

// ---------------------------------------------------------------------------
// messagesProvider
// FutureProvider.family — fetches messages for a specific thread.
// Invalidate with ref.invalidate(messagesProvider(threadId)) after sending.
// ---------------------------------------------------------------------------

/// Returns the message list for [threadId].  Falls back to [] on error.
final messagesProvider =
    FutureProvider.family<List<ChatMessage>, String>((ref, threadId) async {
  final client = ref.read(skcommClientProvider);
  try {
    return await client.getConversationMessages(threadId);
  } catch (_) {
    return [];
  }
});

// ---------------------------------------------------------------------------
// presenceProvider
// StreamProvider — polls GET /api/v1/presence every 30 s.
// ---------------------------------------------------------------------------

/// Emits a map of identity → isOnline, refreshed every 30 seconds.
final presenceProvider = StreamProvider<Map<String, bool>>((ref) async* {
  final client = ref.read(skcommClientProvider);

  Future<Map<String, bool>> fetchPresence() async {
    try {
      final raw = await client.getAllPresence();
      return raw.map(
        (key, value) => MapEntry(
          key,
          value == true || value == 'online',
        ),
      );
    } catch (_) {
      return {};
    }
  }

  // Emit immediately, then every 30 s.
  yield await fetchPresence();
  await for (final _ in Stream.periodic(const Duration(seconds: 30))) {
    yield await fetchPresence();
  }
});

// ---------------------------------------------------------------------------
// sendMessageProvider
// StateNotifierProvider — wraps sendMessage so screens get isSending / error.
// ---------------------------------------------------------------------------

class SendMessageState {
  final bool isSending;
  final String? error;

  const SendMessageState({this.isSending = false, this.error});

  SendMessageState copyWith({bool? isSending, String? error}) {
    return SendMessageState(
      isSending: isSending ?? this.isSending,
      error: error,
    );
  }
}

class SendMessageNotifier extends StateNotifier<SendMessageState> {
  SendMessageNotifier(this._client) : super(const SendMessageState());

  final SKCommClient _client;

  Future<void> sendMessage(String recipient, String content) async {
    state = const SendMessageState(isSending: true);
    try {
      await _client.sendMessage(recipientId: recipient, content: content);
      state = const SendMessageState();
    } on SKCommException catch (e) {
      state = SendMessageState(error: e.message);
    } catch (e) {
      state = SendMessageState(error: e.toString());
    }
  }
}

/// Use `.notifier.sendMessage(recipient, content)` to send.
/// Watch state for `isSending` / `error`.
final sendMessageProvider =
    StateNotifierProvider<SendMessageNotifier, SendMessageState>(
  (ref) => SendMessageNotifier(ref.read(skcommClientProvider)),
);
