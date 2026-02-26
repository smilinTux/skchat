import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:hive_flutter/hive_flutter.dart';
import '../models/chat_message.dart';

/// Persistent message store — one Hive box per peer conversation.
///
/// Box naming: `messages_<peerId>` keeps conversations isolated and
/// avoids scanning the entire dataset for a single thread.
class MessageRepository {
  static const _boxPrefix = 'messages_';

  /// Sanitize peerId into a valid Hive box name (lowercase, no special chars).
  static String _boxName(String peerId) =>
      '$_boxPrefix${peerId.replaceAll(RegExp(r'[^a-zA-Z0-9_]'), '_').toLowerCase()}';

  Future<Box<ChatMessage>> _openBox(String peerId) async {
    final name = _boxName(peerId);
    if (Hive.isBoxOpen(name)) return Hive.box<ChatMessage>(name);
    return Hive.openBox<ChatMessage>(name);
  }

  /// Load all messages for a conversation, sorted oldest-first.
  Future<List<ChatMessage>> getMessages(String peerId) async {
    final box = await _openBox(peerId);
    final messages = box.values.toList()
      ..sort((a, b) => a.timestamp.compareTo(b.timestamp));
    return messages;
  }

  /// Append a message. Uses the message id as the Hive key for idempotency.
  Future<void> saveMessage(ChatMessage message) async {
    final box = await _openBox(message.peerId);
    await box.put(message.id, message);
  }

  /// Batch-save messages (e.g. syncing from daemon inbox).
  Future<void> saveMessages(String peerId, List<ChatMessage> messages) async {
    final box = await _openBox(peerId);
    final map = {for (final m in messages) m.id: m};
    await box.putAll(map);
  }

  /// Update delivery status for a single message.
  Future<void> updateDeliveryStatus(
    String peerId,
    String messageId,
    String status,
  ) async {
    final box = await _openBox(peerId);
    final existing = box.get(messageId);
    if (existing != null) {
      await box.put(messageId, existing.copyWith(deliveryStatus: status));
    }
  }

  /// Number of persisted messages for a peer.
  Future<int> messageCount(String peerId) async {
    final box = await _openBox(peerId);
    return box.length;
  }

  /// Delete all messages for a peer (clear thread).
  Future<void> clearConversation(String peerId) async {
    final box = await _openBox(peerId);
    await box.clear();
  }
}

final messageRepositoryProvider = Provider<MessageRepository>(
  (_) => MessageRepository(),
);
