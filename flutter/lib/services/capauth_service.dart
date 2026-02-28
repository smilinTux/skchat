import 'dart:convert';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:hive_flutter/hive_flutter.dart';

const _kAuthBox = 'capauth_sessions';

// ── Domain models ─────────────────────────────────────────────────────────

/// A CapAuth session cached locally after a successful QR login.
class CapAuthSession {
  const CapAuthSession({
    required this.server,
    required this.fingerprint,
    required this.sessionToken,
    required this.expiresAt,
  });

  final String server;
  final String fingerprint;
  final String sessionToken;
  final DateTime expiresAt;

  bool get isExpired => DateTime.now().isAfter(expiresAt);

  Map<String, dynamic> toJson() => {
        'server': server,
        'fingerprint': fingerprint,
        'session_token': sessionToken,
        'expires_at': expiresAt.toIso8601String(),
      };

  factory CapAuthSession.fromJson(Map<String, dynamic> json) {
    return CapAuthSession(
      server: json['server'] as String,
      fingerprint: json['fingerprint'] as String,
      sessionToken: json['session_token'] as String,
      expiresAt: DateTime.parse(json['expires_at'] as String),
    );
  }
}

/// Parsed payload from a scanned CapAuth QR code.
///
/// Accepted URI formats:
///   capauth://login?server=https://capauth.io&nonce=ABC&fp=FINGERPRINT
///   https://capauth.io/login?nonce=ABC
class CapAuthQrPayload {
  const CapAuthQrPayload({
    required this.server,
    required this.nonce,
    this.fingerprint,
  });

  /// CapAuth server base URL, e.g. https://capauth.io
  final String server;

  /// One-time login nonce / challenge token.
  final String nonce;

  /// Expected PGP fingerprint of the server (optional, for display).
  final String? fingerprint;

  /// Parse a raw QR string. Returns null if not a valid CapAuth URI.
  static CapAuthQrPayload? tryParse(String raw) {
    try {
      final uri = Uri.parse(raw);
      final server = uri.queryParameters['server'] ??
          (uri.scheme.startsWith('http')
              ? '${uri.scheme}://${uri.host}'
              : null);
      final nonce =
          uri.queryParameters['nonce'] ?? uri.queryParameters['challenge'];
      if (server == null || nonce == null || nonce.isEmpty) return null;
      return CapAuthQrPayload(
        server: server,
        nonce: nonce,
        fingerprint: uri.queryParameters['fp'],
      );
    } catch (_) {
      return null;
    }
  }
}

// ── Service ───────────────────────────────────────────────────────────────

/// HTTP client for the CapAuth PGP challenge-response login protocol.
///
/// Login flow:
///   1. Fetch our PGP fingerprint from the local SKComm daemon.
///   2. Ask the daemon to sign the nonce (private key stays in daemon).
///   3. POST {fingerprint, nonce, signature} to the CapAuth server.
///   4. Cache and return the returned session token.
class CapAuthService {
  CapAuthService({required this.skcommBaseUrl});

  final String skcommBaseUrl;

  late final Dio _skcomm = Dio(
    BaseOptions(
      baseUrl: skcommBaseUrl,
      connectTimeout: const Duration(seconds: 5),
      receiveTimeout: const Duration(seconds: 10),
      headers: {'Content-Type': 'application/json'},
    ),
  );

  // ── Login ──────────────────────────────────────────────────────────────

  Future<CapAuthSession> login(CapAuthQrPayload payload) async {
    // 1. Get local fingerprint.
    final identityResp = await _skcomm.get<Map<String, dynamic>>(
      '/api/v1/identity',
    );
    final fingerprint =
        (identityResp.data ?? {})['fingerprint'] as String? ?? '';

    // 2. Sign the nonce via the daemon.
    final signResp = await _skcomm.post<Map<String, dynamic>>(
      '/api/v1/sign',
      data: {'nonce': payload.nonce},
    );
    final signature =
        (signResp.data ?? {})['signature'] as String? ?? '';

    // 3. POST to CapAuth server.
    final capAuthDio = Dio(
      BaseOptions(
        baseUrl: payload.server,
        connectTimeout: const Duration(seconds: 10),
        receiveTimeout: const Duration(seconds: 15),
        headers: {'Content-Type': 'application/json'},
      ),
    );
    final authResp = await capAuthDio.post<Map<String, dynamic>>(
      '/api/v1/auth/verify',
      data: {
        'fingerprint': fingerprint,
        'nonce': payload.nonce,
        'signature': signature,
      },
    );
    final data = authResp.data ?? {};
    final token = data['session_token'] as String? ?? '';
    final expiresAt = data['expires_at'] != null
        ? DateTime.parse(data['expires_at'] as String)
        : DateTime.now().add(const Duration(hours: 24));

    final session = CapAuthSession(
      server: payload.server,
      fingerprint: fingerprint,
      sessionToken: token,
      expiresAt: expiresAt,
    );

    await _cacheSession(session);
    return session;
  }

  // ── Session cache ──────────────────────────────────────────────────────

  Future<void> _cacheSession(CapAuthSession session) async {
    final box = await Hive.openBox<String>(_kAuthBox);
    await box.put(session.server, jsonEncode(session.toJson()));
  }

  Future<CapAuthSession?> loadCachedSession(String server) async {
    final box = await Hive.openBox<String>(_kAuthBox);
    final raw = box.get(server);
    if (raw == null) return null;
    try {
      final session =
          CapAuthSession.fromJson(jsonDecode(raw) as Map<String, dynamic>);
      if (session.isExpired) {
        await box.delete(server);
        return null;
      }
      return session;
    } catch (_) {
      return null;
    }
  }

  Future<void> clearSession(String server) async {
    final box = await Hive.openBox<String>(_kAuthBox);
    await box.delete(server);
  }

  /// Returns all non-expired cached sessions.
  Future<List<CapAuthSession>> loadAllSessions() async {
    final box = await Hive.openBox<String>(_kAuthBox);
    final sessions = <CapAuthSession>[];
    for (final key in box.keys) {
      final raw = box.get(key as String);
      if (raw == null) continue;
      try {
        final s =
            CapAuthSession.fromJson(jsonDecode(raw) as Map<String, dynamic>);
        if (!s.isExpired) sessions.add(s);
      } catch (_) {}
    }
    return sessions;
  }
}

// ── Provider ──────────────────────────────────────────────────────────────

final capAuthServiceProvider = Provider<CapAuthService>((ref) {
  return CapAuthService(skcommBaseUrl: 'http://localhost:9384');
});
