import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../models/chat_message.dart';
import '../../models/conversation.dart';

/// HTTP client for SKComm daemon communication
/// Talks to localhost:9384 for all messaging operations
class SKCommClient {
  final Dio _dio;
  final String baseUrl;

  SKCommClient({
    String? baseUrl,
    Dio? dio,
  })  : baseUrl = baseUrl ?? 'http://localhost:9384',
        _dio = dio ??
            Dio(BaseOptions(
              connectTimeout: const Duration(seconds: 5),
              receiveTimeout: const Duration(seconds: 10),
              headers: {
                'Content-Type': 'application/json',
              },
            ));

  /// Send a message
  Future<void> sendMessage({
    required String recipientId,
    required String content,
    String? replyToId,
    int? ttl,
  }) async {
    try {
      await _dio.post(
        '$baseUrl/api/v1/send',
        data: {
          'recipient_id': recipientId,
          'content': content,
          if (replyToId != null) 'reply_to_id': replyToId,
          if (ttl != null) 'ttl': ttl,
        },
      );
    } on DioException catch (e) {
      throw SKCommException('Failed to send message: ${e.message}');
    }
  }

  /// Poll for new messages
  Future<List<ChatMessage>> pollInbox() async {
    try {
      final response = await _dio.get('$baseUrl/api/v1/inbox');
      final messages = (response.data as List)
          .map((json) => ChatMessage.fromJson(json as Map<String, dynamic>))
          .toList();
      return messages;
    } on DioException catch (e) {
      throw SKCommException('Failed to poll inbox: ${e.message}');
    }
  }

  /// Get list of conversations
  Future<List<Conversation>> getConversations() async {
    try {
      final response = await _dio.get('$baseUrl/api/v1/conversations');
      final conversations = (response.data as List)
          .map((json) => Conversation.fromJson(json as Map<String, dynamic>))
          .toList();
      return conversations;
    } on DioException catch (e) {
      throw SKCommException('Failed to get conversations: ${e.message}');
    }
  }

  /// Get messages for a specific conversation
  Future<List<ChatMessage>> getConversationMessages(String conversationId) async {
    try {
      final response = await _dio.get(
        '$baseUrl/api/v1/conversation/$conversationId',
      );
      final messages = (response.data as List)
          .map((json) => ChatMessage.fromJson(json as Map<String, dynamic>))
          .toList();
      return messages;
    } on DioException catch (e) {
      throw SKCommException('Failed to get conversation messages: ${e.message}');
    }
  }

  /// Broadcast presence status
  Future<void> broadcastPresence({
    required String status,
    String? customMessage,
  }) async {
    try {
      await _dio.post(
        '$baseUrl/api/v1/presence',
        data: {
          'status': status,
          if (customMessage != null) 'custom_message': customMessage,
        },
      );
    } on DioException catch (e) {
      throw SKCommException('Failed to broadcast presence: ${e.message}');
    }
  }

  /// Get peer presence status
  Future<Map<String, dynamic>> getPeerPresence(String peerId) async {
    try {
      final response = await _dio.get('$baseUrl/api/v1/presence/$peerId');
      return response.data as Map<String, dynamic>;
    } on DioException catch (e) {
      throw SKCommException('Failed to get peer presence: ${e.message}');
    }
  }

  /// Get transport health status
  Future<Map<String, dynamic>> getTransportStatus() async {
    try {
      final response = await _dio.get('$baseUrl/api/v1/status');
      return response.data as Map<String, dynamic>;
    } on DioException catch (e) {
      throw SKCommException('Failed to get transport status: ${e.message}');
    }
  }

  /// Get local identity information
  Future<Map<String, dynamic>> getIdentity() async {
    try {
      final response = await _dio.get('$baseUrl/api/v1/identity');
      return response.data as Map<String, dynamic>;
    } on DioException catch (e) {
      throw SKCommException('Failed to get identity: ${e.message}');
    }
  }

  /// Send message to group
  Future<void> sendGroupMessage({
    required String groupId,
    required String content,
    String? replyToId,
  }) async {
    try {
      await _dio.post(
        '$baseUrl/api/v1/groups/$groupId/send',
        data: {
          'content': content,
          if (replyToId != null) 'reply_to_id': replyToId,
        },
      );
    } on DioException catch (e) {
      throw SKCommException('Failed to send group message: ${e.message}');
    }
  }

  /// Get list of known agents
  Future<List<Map<String, dynamic>>> getAgents() async {
    try {
      final response = await _dio.get('$baseUrl/api/v1/agents');
      return (response.data as List)
          .cast<Map<String, dynamic>>()
          .toList();
    } on DioException catch (e) {
      throw SKCommException('Failed to get agents: ${e.message}');
    }
  }
}

/// Exception for SKComm client errors
class SKCommException implements Exception {
  final String message;
  SKCommException(this.message);

  @override
  String toString() => 'SKCommException: $message';
}

/// Provider for SKComm client
final skcommClientProvider = Provider<SKCommClient>((ref) {
  return SKCommClient();
});
