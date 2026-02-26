import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

/// Low-level HTTP client wrapping the SKComm daemon REST API.
/// Default base URL is localhost:9384 — the daemon runs on the same device.
class SKCommClient {
  SKCommClient({String? baseUrl})
    : _dio = Dio(
        BaseOptions(
          baseUrl: baseUrl ?? 'http://localhost:9384',
          connectTimeout: const Duration(seconds: 5),
          receiveTimeout: const Duration(seconds: 10),
          headers: {'Content-Type': 'application/json'},
        ),
      );

  final Dio _dio;

  // ── Health ────────────────────────────────────────────────────────────────

  /// GET / — verify the daemon is running.
  Future<bool> isAlive() async {
    try {
      final resp = await _dio.get('/');
      return resp.statusCode == 200;
    } catch (_) {
      return false;
    }
  }

  /// GET /api/v1/status — full transport health report.
  Future<Map<String, dynamic>> getStatus() async {
    final resp = await _dio.get('/api/v1/status');
    return resp.data as Map<String, dynamic>;
  }

  // ── Messaging ─────────────────────────────────────────────────────────────

  /// POST /api/v1/send — send a message to a peer.
  ///
  /// [recipient] is the peer's name or fingerprint (e.g. 'lumina').
  /// [message] is the plaintext content.
  /// Returns the envelope ID on success.
  Future<SendResult> sendMessage({
    required String recipient,
    required String message,
    String? threadId,
    String? inReplyTo,
  }) async {
    final body = {
      'recipient': recipient,
      'message': message,
      'thread_id': threadId,
      'in_reply_to': inReplyTo,
    };
    final resp = await _dio.post('/api/v1/send', data: body);
    final data = resp.data as Map<String, dynamic>;
    return SendResult(
      delivered: data['delivered'] as bool? ?? false,
      envelopeId: data['envelope_id'] as String? ?? '',
      transportUsed: data['transport_used'] as String?,
    );
  }

  /// GET /api/v1/inbox — poll for new incoming messages.
  Future<List<InboxMessage>> getInbox() async {
    final resp = await _dio.get('/api/v1/inbox');
    final list = resp.data as List<dynamic>;
    return list
        .map((e) => InboxMessage.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// GET /api/v1/conversations — list known conversations.
  Future<List<Map<String, dynamic>>> getConversations() async {
    final resp = await _dio.get('/api/v1/conversations');
    return (resp.data as List<dynamic>)
        .map((e) => e as Map<String, dynamic>)
        .toList();
  }

  // ── Peers ─────────────────────────────────────────────────────────────────

  /// GET /api/v1/peers — list all known peers.
  Future<List<PeerInfo>> getPeers() async {
    final resp = await _dio.get('/api/v1/peers');
    final list = resp.data as List<dynamic>;
    return list
        .map((e) => PeerInfo.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  // ── Agents ────────────────────────────────────────────────────────────────

  /// GET /api/v1/agents — list known agents.
  Future<List<Map<String, dynamic>>> getAgents() async {
    final resp = await _dio.get('/api/v1/agents');
    return (resp.data as List<dynamic>)
        .map((e) => e as Map<String, dynamic>)
        .toList();
  }

  // ── Presence ──────────────────────────────────────────────────────────────

  /// POST /api/v1/presence — broadcast presence status.
  Future<void> updatePresence({
    required String status,
    String? message,
  }) async {
    await _dio.post('/api/v1/presence', data: {
      'status': status,
      'message': message,
    });
  }
}

// ── Data transfer objects ──────────────────────────────────────────────────

class SendResult {
  const SendResult({
    required this.delivered,
    required this.envelopeId,
    this.transportUsed,
  });

  final bool delivered;
  final String envelopeId;
  final String? transportUsed;
}

class InboxMessage {
  const InboxMessage({
    required this.envelopeId,
    required this.sender,
    required this.recipient,
    required this.content,
    required this.createdAt,
    this.threadId,
    this.inReplyTo,
    this.isEncrypted = true,
  });

  final String envelopeId;
  final String sender;
  final String recipient;
  final String content;
  final DateTime createdAt;
  final String? threadId;
  final String? inReplyTo;
  final bool isEncrypted;

  factory InboxMessage.fromJson(Map<String, dynamic> json) {
    return InboxMessage(
      envelopeId: json['envelope_id'] as String? ?? '',
      sender: json['sender'] as String? ?? '',
      recipient: json['recipient'] as String? ?? '',
      content: json['content'] as String? ?? '',
      createdAt: json['created_at'] != null
          ? DateTime.parse(json['created_at'] as String)
          : DateTime.now(),
      threadId: json['thread_id'] as String?,
      inReplyTo: json['in_reply_to'] as String?,
      isEncrypted: json['encrypted'] as bool? ?? true,
    );
  }
}

class PeerInfo {
  const PeerInfo({
    required this.name,
    this.fingerprint,
    this.lastSeen,
    this.transports = const [],
  });

  final String name;
  final String? fingerprint;
  final DateTime? lastSeen;
  final List<String> transports;

  factory PeerInfo.fromJson(Map<String, dynamic> json) {
    final transports = <String>[];
    if (json['transports'] is List) {
      for (final t in json['transports'] as List) {
        if (t is Map) transports.add(t['transport'] as String? ?? '');
      }
    }
    return PeerInfo(
      name: json['name'] as String? ?? '',
      fingerprint: json['fingerprint'] as String?,
      lastSeen: json['last_seen'] != null
          ? DateTime.tryParse(json['last_seen'] as String)
          : null,
      transports: transports,
    );
  }
}

// ── Riverpod provider ──────────────────────────────────────────────────────

/// Singleton SKCommClient — base URL can be overridden for testing.
final skcommClientProvider = Provider<SKCommClient>((ref) {
  return SKCommClient();
});
