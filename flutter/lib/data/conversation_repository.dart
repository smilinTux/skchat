import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:hive_flutter/hive_flutter.dart';
import '../models/conversation.dart';

/// Persistent conversation list store — single Hive box for all threads.
class ConversationRepository {
  static const _boxName = 'conversations';

  Future<Box<Conversation>> _openBox() async {
    if (Hive.isBoxOpen(_boxName)) return Hive.box<Conversation>(_boxName);
    return Hive.openBox<Conversation>(_boxName);
  }

  /// Load all conversations, sorted by most recent message first.
  Future<List<Conversation>> getAll() async {
    final box = await _openBox();
    final convos = box.values.toList()
      ..sort((a, b) => b.lastMessageTime.compareTo(a.lastMessageTime));
    return convos;
  }

  /// Save or update a conversation (keyed by peerId).
  Future<void> save(Conversation conversation) async {
    final box = await _openBox();
    await box.put(conversation.peerId, conversation);
  }

  /// Batch-save (e.g. after daemon sync).
  Future<void> saveAll(List<Conversation> conversations) async {
    final box = await _openBox();
    final map = {for (final c in conversations) c.peerId: c};
    await box.putAll(map);
  }

  /// Remove a conversation entirely.
  Future<void> delete(String peerId) async {
    final box = await _openBox();
    await box.delete(peerId);
  }

  /// Check if any persisted conversations exist.
  Future<bool> get isEmpty async {
    final box = await _openBox();
    return box.isEmpty;
  }
}

final conversationRepositoryProvider = Provider<ConversationRepository>(
  (_) => ConversationRepository(),
);
