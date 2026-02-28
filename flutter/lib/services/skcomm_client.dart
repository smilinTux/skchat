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

  // ── Groups ──────────────────────────────────────────────────────────────

  /// GET /api/v1/groups/:groupId/members — list members of a group.
  Future<List<Map<String, dynamic>>> getGroupMembers(String groupId) async {
    final resp = await _dio.get('/api/v1/groups/$groupId/members');
    return (resp.data as List<dynamic>)
        .map((e) => e as Map<String, dynamic>)
        .toList();
  }

  /// POST /api/v1/groups — create a new group chat.
  ///
  /// [name] is required. [description] and [memberUris] are optional.
  /// Returns a [CreateGroupResult] with the group ID and AES-256-GCM key info.
  Future<CreateGroupResult> createGroup({
    required String name,
    String? description,
    List<String> memberUris = const [],
  }) async {
    final body = <String, dynamic>{'name': name};
    if (description != null && description.isNotEmpty) {
      body['description'] = description;
    }
    if (memberUris.isNotEmpty) {
      body['members'] =
          memberUris.map((u) => <String, dynamic>{'identity': u}).toList();
    }
    final resp = await _dio.post('/api/v1/groups', data: body);
    return CreateGroupResult.fromJson(resp.data as Map<String, dynamic>);
  }

  /// POST /api/v1/groups/:groupId/members — add a member.
  Future<void> addGroupMember(
    String groupId, {
    required String identity,
    String role = 'member',
  }) async {
    await _dio.post(
      '/api/v1/groups/$groupId/members',
      data: {'identity': identity, 'role': role},
    );
  }

  /// DELETE /api/v1/groups/:groupId/members/:identity — remove a member.
  Future<void> removeGroupMember(String groupId, String identity) async {
    await _dio.delete('/api/v1/groups/$groupId/members/$identity');
  }

  /// PUT /api/v1/groups/:groupId — update group name or description.
  Future<void> updateGroupInfo(
    String groupId, {
    String? name,
    String? description,
  }) async {
    final body = <String, dynamic>{};
    if (name != null) body['name'] = name;
    if (description != null) body['description'] = description;
    await _dio.put('/api/v1/groups/$groupId', data: body);
  }

  /// DELETE /api/v1/groups/:groupId/members/self — leave a group.
  Future<void> leaveGroup(String groupId) async {
    await _dio.delete('/api/v1/groups/$groupId/members/self');
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

  // ── Identity ──────────────────────────────────────────────────────────────

  /// GET /api/v1/identity — return this node's PGP fingerprint and name.
  Future<IdentityInfo> getIdentity() async {
    final resp = await _dio.get<Map<String, dynamic>>('/api/v1/identity');
    return IdentityInfo.fromJson(resp.data ?? {});
  }

  // ── WebRTC ────────────────────────────────────────────────────────────────

  /// GET /api/v1/webrtc/ice-config — ICE server list with TURN credentials.
  ///
  /// Returns the list ready to pass to RTCPeerConnection config['iceServers'].
  /// Falls back to Google STUN when the daemon is unreachable.
  Future<List<Map<String, dynamic>>> getIceConfig() async {
    try {
      final resp = await _dio.get<Map<String, dynamic>>(
        '/api/v1/webrtc/ice-config',
      );
      final data = resp.data ?? {};
      final servers = data['ice_servers'] as List<dynamic>? ?? [];
      return servers
          .whereType<Map>()
          .map((s) => Map<String, dynamic>.from(s))
          .toList();
    } catch (_) {
      return [
        {'urls': 'stun:stun.l.google.com:19302'},
      ];
    }
  }

  /// GET /api/v1/webrtc/peers — list peers in a signaling room.
  Future<Map<String, dynamic>> getWebRTCPeers({String? room}) async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/api/v1/webrtc/peers',
      queryParameters: room != null ? {'room': room} : null,
    );
    return resp.data ?? {};
  }

  // ── Signing ───────────────────────────────────────────────────────────────

  /// POST /api/v1/sign — ask the daemon to sign [nonce] with the local PGP key.
  ///
  /// The private key never leaves the daemon; the app only receives the
  /// armored PGP signature.
  Future<String> signNonce(String nonce) async {
    final resp = await _dio.post<Map<String, dynamic>>(
      '/api/v1/sign',
      data: {'nonce': nonce},
    );
    return (resp.data ?? {})['signature'] as String? ?? '';
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

class CreateGroupResult {
  const CreateGroupResult({
    required this.groupId,
    required this.name,
    this.description,
    this.memberCount = 0,
    this.keyId,
    this.keyAlgorithm = 'AES-256-GCM',
    this.members = const [],
  });

  final String groupId;
  final String name;
  final String? description;
  final int memberCount;
  /// Short identifier of the group encryption key.
  final String? keyId;
  final String keyAlgorithm;
  final List<String> members;

  factory CreateGroupResult.fromJson(Map<String, dynamic> json) {
    final rawMembers = json['members'] as List<dynamic>? ?? [];
    return CreateGroupResult(
      groupId: json['group_id'] as String? ?? '',
      name: json['name'] as String? ?? '',
      description: json['description'] as String?,
      memberCount: json['member_count'] as int? ?? 0,
      keyId: json['key_id'] as String?,
      keyAlgorithm: json['key_algorithm'] as String? ?? 'AES-256-GCM',
      members: rawMembers
          .map((e) =>
              (e is Map ? e['identity'] as String? : e as String?) ?? '')
          .where((s) => s.isNotEmpty)
          .toList(),
    );
  }
}

class IdentityInfo {
  const IdentityInfo({
    required this.fingerprint,
    this.name,
    this.email,
  });

  final String fingerprint;
  final String? name;
  final String? email;

  factory IdentityInfo.fromJson(Map<String, dynamic> json) {
    return IdentityInfo(
      fingerprint: json['fingerprint'] as String? ?? '',
      name: json['name'] as String?,
      email: json['email'] as String?,
    );
  }
}

// ── Riverpod provider ──────────────────────────────────────────────────────

/// Singleton SKCommClient — base URL can be overridden for testing.
final skcommClientProvider = Provider<SKCommClient>((ref) {
  return SKCommClient();
});
