import 'dart:convert';
import 'dart:io';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

// ── Data Transfer Objects ──────────────────────────────────────────────────

/// A message loaded from the skchat local history store via CLI.
///
/// Mirrors the JSON shape emitted by `skchat inbox --json`:
/// ```json
/// {"sender": "capauth:lumina@skworld.io", "recipient": "capauth:opus@skworld.io",
///  "content": "hello", "thread_id": null, "timestamp": "2026-03-03T..."}
/// ```
class SkchatCliMessage {
  const SkchatCliMessage({
    required this.sender,
    required this.recipient,
    required this.content,
    required this.timestamp,
    this.threadId,
  });

  final String sender;
  final String recipient;
  final String content;
  final DateTime timestamp;
  final String? threadId;

  /// Stable dedup key derived from sender + timestamp milliseconds.
  String get id => '${sender}_${timestamp.millisecondsSinceEpoch}';

  factory SkchatCliMessage.fromJson(Map<String, dynamic> json) {
    final rawTs = json['timestamp'];
    final DateTime ts;
    if (rawTs is String) {
      ts = DateTime.tryParse(rawTs) ?? DateTime.now();
    } else {
      ts = DateTime.now();
    }
    return SkchatCliMessage(
      sender: json['sender'] as String? ?? '',
      recipient: json['recipient'] as String? ?? '',
      content: json['content'] as String? ?? '',
      timestamp: ts,
      threadId: json['thread_id'] as String?,
    );
  }
}

/// Result of a `skchat send` CLI call.
class DaemonSendResult {
  const DaemonSendResult({required this.success, this.error});
  final bool success;
  final String? error;
}

// ── DaemonService ──────────────────────────────────────────────────────────

/// Bridges the Flutter UI to the local skchat daemon.
///
/// Two channels:
/// - **HTTP** – health check at `localhost:9385/health` (daemon's built-in server).
/// - **CLI**  – `skchat inbox --json` / `skchat send` via dart:io [Process].
///
/// All CLI calls run from [workingDir] (default: `$HOME`) to avoid the
/// skmemory namespace collision that occurs when CWD is the project root.
class DaemonService {
  DaemonService({
    String? healthBaseUrl,
    String? workingDir,
  })  : _healthBaseUrl = healthBaseUrl ?? 'http://127.0.0.1:9385',
        _workingDir =
            workingDir ?? Platform.environment['HOME'] ?? '/home/${Platform.environment['USER'] ?? 'user'}',
        _dio = Dio(
          BaseOptions(
            connectTimeout: const Duration(seconds: 3),
            receiveTimeout: const Duration(seconds: 5),
          ),
        );

  final String _healthBaseUrl;
  final String _workingDir;
  final Dio _dio;

  /// The local skchat identity URI from the environment, e.g.
  /// `capauth:opus@skworld.io`.  Used to classify messages as outbound.
  String? get localIdentity => Platform.environment['SKCHAT_IDENTITY'];

  /// Extract the short peer name from a CapAuth URI.
  /// `capauth:lumina@skworld.io` → `lumina`
  static String peerShortName(String uri) {
    // Strip scheme prefix if present.
    var s = uri.startsWith('capauth:') ? uri.substring('capauth:'.length) : uri;
    // Take only the local part before '@'.
    return s.split('@').first;
  }

  // ── Health ────────────────────────────────────────────────────────────────

  /// Returns `true` if the skchat daemon health endpoint is reachable.
  Future<bool> isAlive() async {
    try {
      final resp = await _dio.get('$_healthBaseUrl/health');
      return resp.statusCode == 200;
    } catch (_) {
      return false;
    }
  }

  /// Full health payload from the daemon (`GET /health`).
  Future<Map<String, dynamic>?> getHealth() async {
    try {
      final resp =
          await _dio.get<Map<String, dynamic>>('$_healthBaseUrl/health');
      return resp.data;
    } catch (_) {
      return null;
    }
  }

  // ── Inbox ─────────────────────────────────────────────────────────────────

  /// Fetch all messages from the local skchat history store via CLI.
  ///
  /// Runs: `skchat inbox --json --limit <limit>` from `$HOME`.
  ///
  /// Returns an empty list on error (daemon not running, CLI not in PATH, etc.).
  Future<List<SkchatCliMessage>> getInbox({int limit = 100}) async {
    try {
      final result = await Process.run(
        'skchat',
        ['inbox', '--json', '--limit', '$limit'],
        workingDirectory: _workingDir,
        stdoutEncoding: utf8,
        stderrEncoding: utf8,
      );
      if (result.exitCode != 0) return [];

      final stdout = result.stdout as String;
      if (stdout.trim().isEmpty) return [];

      final decoded = jsonDecode(stdout);
      if (decoded is! List) return [];

      return decoded
          .whereType<Map<String, dynamic>>()
          .map(SkchatCliMessage.fromJson)
          .toList();
    } catch (_) {
      return [];
    }
  }

  /// Fetch messages exchanged with a specific peer from the local store.
  ///
  /// Filters the full inbox client-side by sender/recipient matching [peerId].
  /// [peerId] may be a short name (`lumina`) or full URI
  /// (`capauth:lumina@skworld.io`).
  Future<List<SkchatCliMessage>> getConversation(
    String peerId, {
    int limit = 100,
  }) async {
    final all = await getInbox(limit: limit * 3);
    final short = peerShortName(peerId).toLowerCase();
    return all.where((m) {
      final senderShort = peerShortName(m.sender).toLowerCase();
      final recipientShort = peerShortName(m.recipient).toLowerCase();
      return senderShort == short || recipientShort == short;
    }).take(limit).toList();
  }

  // ── Send ──────────────────────────────────────────────────────────────────

  /// Send a message via `skchat send <recipient> <content>` CLI.
  ///
  /// Stores the message in the local skchat history AND delivers it via the
  /// configured SKComm transport.  Runs from `$HOME`.
  Future<DaemonSendResult> sendMessage({
    required String recipient,
    required String content,
  }) async {
    try {
      final result = await Process.run(
        'skchat',
        ['send', recipient, content],
        workingDirectory: _workingDir,
        stdoutEncoding: utf8,
        stderrEncoding: utf8,
      );
      if (result.exitCode == 0) {
        return const DaemonSendResult(success: true);
      }
      return DaemonSendResult(
        success: false,
        error: (result.stderr as String).trim(),
      );
    } catch (e) {
      return DaemonSendResult(success: false, error: e.toString());
    }
  }
}

// ── Riverpod provider ──────────────────────────────────────────────────────

/// Singleton [DaemonService] instance.
final daemonServiceProvider = Provider<DaemonService>((ref) {
  return DaemonService();
});
