import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../models/chat_message.dart';

/// Default base URL for the SKComm daemon.
/// Override at runtime via the SKCOMM_URL environment variable
/// or by passing [baseUrl] to the constructor.
const _kDefaultBaseUrl = 'http://localhost:9384';

/// HTTP client for SKComm daemon communication
/// Talks to the SKComm daemon for all messaging operations
class SKCommClient {
  final Dio _dio;
  final String baseUrl;

  SKCommClient({
    String? baseUrl,
    Dio? dio,
  })  : baseUrl = baseUrl ??
            const String.fromEnvironment('SKCOMM_URL',
                defaultValue: _kDefaultBaseUrl),
        _dio = dio ??
            Dio(BaseOptions(
              connectTimeout: const Duration(seconds: 5),
              receiveTimeout: const Duration(seconds: 10),
              headers: {
                'Content-Type': 'application/json',
              },
            ));

  /// Send a message
  /// POST /api/v1/send — body: {recipient, message}
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
          'recipient': recipientId,
          'message': content,
          if (replyToId != null) 'in_reply_to': replyToId,
        },
      );
    } on DioException catch (e) {
      throw SKCommException('Failed to send message: ${e.message}');
    }
  }

  /// Poll for new messages from GET /api/v1/inbox.
  /// Maps MessageEnvelopeResponse fields to ChatMessage.
  Future<List<ChatMessage>> pollInbox() async {
    try {
      final response = await _dio.get('$baseUrl/api/v1/inbox');
      final list = response.data as List? ?? [];
      return list.map((json) {
        final j = json as Map<String, dynamic>;
        return ChatMessage(
          id: j['envelope_id'] as String? ?? '',
          conversationId:
              j['thread_id'] as String? ?? j['sender'] as String? ?? '',
          senderId: j['sender'] as String? ?? '',
          senderName: j['sender'] as String? ?? '',
          content: j['content'] as String? ?? '',
          timestamp: j['created_at'] != null
              ? DateTime.parse(j['created_at'] as String)
              : DateTime.now(),
          isEncrypted: j['encrypted'] as bool? ?? false,
          status: MessageStatus.delivered,
          replyToId: j['in_reply_to'] as String?,
        );
      }).toList();
    } on DioException catch (e) {
      throw SKCommException('Failed to poll inbox: ${e.message}');
    }
  }

  /// Get list of conversations (raw JSON maps for caller parsing).
  /// GET /api/v1/conversations
  Future<List<Map<String, dynamic>>> getConversations() async {
    try {
      final response = await _dio.get('$baseUrl/api/v1/conversations');
      return (response.data as List).cast<Map<String, dynamic>>().toList();
    } on DioException catch (e) {
      throw SKCommException('Failed to get conversations: ${e.message}');
    }
  }

  /// Get messages for a specific conversation.
  /// GET /api/v1/conversations/{id}/messages — returns ChatMessageItem list.
  Future<List<ChatMessage>> getConversationMessages(
      String conversationId) async {
    try {
      final response = await _dio.get(
        '$baseUrl/api/v1/conversations/$conversationId/messages',
      );
      final data = response.data as Map<String, dynamic>;
      final messages = data['messages'] as List? ?? [];
      return messages.map((json) {
        final j = json as Map<String, dynamic>;
        return ChatMessage(
          id: j['id'] as String? ?? '',
          conversationId: j['thread_id'] as String? ?? conversationId,
          senderId: j['sender'] as String? ?? '',
          senderName: j['sender'] as String? ?? '',
          content: j['content'] as String? ?? '',
          timestamp: j['timestamp'] != null
              ? DateTime.parse(j['timestamp'] as String)
              : DateTime.now(),
          isEncrypted: j['encrypted'] as bool? ?? false,
          status: MessageStatus.delivered,
          replyToId: j['reply_to'] as String?,
        );
      }).toList();
    } on DioException catch (e) {
      throw SKCommException('Failed to get conversation messages: ${e.message}');
    }
  }

  /// Get presence status for all known peers.
  /// Derived from GET /api/v1/agents — online when last_seen < 5 min ago.
  Future<Map<String, dynamic>> getAllPresence() async {
    try {
      final response = await _dio.get('$baseUrl/api/v1/agents');
      final agents = response.data as List? ?? [];
      final now = DateTime.now();
      final result = <String, dynamic>{};
      for (final item in agents) {
        if (item is! Map<String, dynamic>) continue;
        final name = item['name'] as String? ?? '';
        if (name.isEmpty) continue;
        final lastSeenStr = item['last_seen'] as String?;
        var online = false;
        if (lastSeenStr != null) {
          final lastSeen = DateTime.tryParse(lastSeenStr);
          if (lastSeen != null) {
            online = now.difference(lastSeen).inMinutes < 5;
          }
        }
        result[name] = online ? 'online' : 'offline';
      }
      return result;
    } on DioException catch (e) {
      throw SKCommException('Failed to get presence: ${e.message}');
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
          if (customMessage != null) 'message': customMessage,
        },
      );
    } on DioException catch (e) {
      throw SKCommException('Failed to broadcast presence: ${e.message}');
    }
  }

  /// Get peer presence status.
  /// No dedicated GET endpoint; callers handle errors gracefully.
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

  /// Get local identity information from GET /api/v1/status.
  Future<Map<String, dynamic>> getIdentity() async {
    try {
      final response = await _dio.get('$baseUrl/api/v1/status');
      final data = response.data as Map<String, dynamic>;
      final identity = data['identity'] as Map<String, dynamic>? ?? {};
      return {
        'agent_name': identity['name'] ?? 'unknown',
        'fingerprint': identity['fingerprint'],
        ...identity,
      };
    } on DioException catch (e) {
      throw SKCommException('Failed to get identity: ${e.message}');
    }
  }

  /// Send message to a group via the regular send endpoint.
  Future<void> sendGroupMessage({
    required String groupId,
    required String content,
    String? replyToId,
  }) async {
    try {
      await _dio.post(
        '$baseUrl/api/v1/send',
        data: {
          'recipient': groupId,
          'message': content,
          if (replyToId != null) 'in_reply_to': replyToId,
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
      return (response.data as List).cast<Map<String, dynamic>>().toList();
    } on DioException catch (e) {
      throw SKCommException('Failed to get agents: ${e.message}');
    }
  }

  /// Get list of groups.
  /// Returns empty list when GET /api/v1/groups returns 404.
  Future<List<Map<String, dynamic>>> getGroups() async {
    try {
      final response = await _dio.get('$baseUrl/api/v1/groups');
      return (response.data as List).cast<Map<String, dynamic>>().toList();
    } on DioException catch (e) {
      if (e.response?.statusCode == 404) return [];
      throw SKCommException('Failed to get groups: ${e.message}');
    }
  }

  /// Get trust information for a specific peer
  Future<Map<String, dynamic>> getTrustInfo(String peerId) async {
    try {
      final response = await _dio.get('$baseUrl/api/v1/trust/$peerId');
      return response.data as Map<String, dynamic>;
    } on DioException catch (e) {
      throw SKCommException('Failed to get trust info: ${e.message}');
    }
  }

  /// Search or list memory entries
  Future<List<Map<String, dynamic>>> getMemoryEntries({String? query}) async {
    try {
      final response = await _dio.get(
        '$baseUrl/api/v1/memory',
        queryParameters: query != null ? {'q': query} : null,
      );
      return (response.data as List).cast<Map<String, dynamic>>().toList();
    } on DioException catch (e) {
      throw SKCommException('Failed to get memory entries: ${e.message}');
    }
  }

  /// Store a new memory entry
  Future<void> storeMemory({
    required String content,
    List<String>? tags,
    String? scope,
  }) async {
    try {
      await _dio.post(
        '$baseUrl/api/v1/memory',
        data: {
          'content': content,
          if (tags != null) 'tags': tags,
          if (scope != null) 'scope': scope,
        },
      );
    } on DioException catch (e) {
      throw SKCommException('Failed to store memory: ${e.message}');
    }
  }

  /// Full-text search across stored chat messages.
  Future<List<Map<String, dynamic>>> searchMessages(String query) async {
    try {
      final response = await _dio.get(
        '$baseUrl/api/v1/search',
        queryParameters: {'q': query},
      );
      return (response.data as List).cast<Map<String, dynamic>>();
    } on DioException catch (e) {
      throw SKCommException('Failed to search messages: ${e.message}');
    }
  }

  /// Send a file attachment via multipart upload.
  Future<void> sendFile({
    required String recipientId,
    required String filePath,
    required String fileName,
  }) async {
    try {
      final formData = FormData.fromMap({
        'recipient_id': recipientId,
        'attachment':
            await MultipartFile.fromFile(filePath, filename: fileName),
      });
      await _dio.post('$baseUrl/api/v1/send/file', data: formData);
    } on DioException catch (e) {
      throw SKCommException('Failed to send file: ${e.message}');
    }
  }

  /// Send a voice message via multipart upload.
  Future<void> sendVoiceMessage({
    required String recipientId,
    required String audioPath,
    required int durationMs,
  }) async {
    try {
      final formData = FormData.fromMap({
        'recipient_id': recipientId,
        'duration_ms': durationMs,
        'audio':
            await MultipartFile.fromFile(audioPath, filename: 'voice.m4a'),
      });
      await _dio.post('$baseUrl/api/v1/send/voice', data: formData);
    } on DioException catch (e) {
      throw SKCommException('Failed to send voice message: ${e.message}');
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
