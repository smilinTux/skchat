import 'dart:async';
import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

/// Base URL for the skcapstone daemon (port 7777).
/// Override at build time via --dart-define=SKCAPSTONE_URL=http://host:port
const _kSKCapstoneBaseUrl = String.fromEnvironment(
  'SKCAPSTONE_URL',
  defaultValue: 'http://localhost:7777',
);

/// Base URL for the skcapstone dashboard service (port 7778).
/// Override via --dart-define=SKCAPSTONE_DASHBOARD_URL=http://host:port
const _kSKDashboardBaseUrl = String.fromEnvironment(
  'SKCAPSTONE_DASHBOARD_URL',
  defaultValue: 'http://localhost:7778',
);

/// Low-level HTTP client for the skcapstone daemon REST API.
class SKCapstoneClient {
  SKCapstoneClient({String? baseUrl, String? dashboardUrl})
      : _dio = Dio(
          BaseOptions(
            baseUrl: baseUrl ?? _kSKCapstoneBaseUrl,
            connectTimeout: const Duration(seconds: 5),
            receiveTimeout: const Duration(seconds: 10),
          ),
        ),
        _dashDio = Dio(
          BaseOptions(
            baseUrl: dashboardUrl ?? _kSKDashboardBaseUrl,
            connectTimeout: const Duration(seconds: 5),
            receiveTimeout: const Duration(seconds: 10),
          ),
        );

  final Dio _dio;
  final Dio _dashDio;

  /// GET /ping — verify the daemon is running.
  Future<bool> isAlive() async {
    try {
      final resp = await _dio.get('/ping');
      return resp.statusCode == 200;
    } catch (_) {
      return false;
    }
  }

  /// GET /consciousness — full consciousness loop status.
  Future<Map<String, dynamic>?> getConsciousness() async {
    try {
      final resp = await _dio.get<Map<String, dynamic>>('/consciousness');
      return resp.data;
    } catch (_) {
      return null;
    }
  }

  /// GET /api/v1/conversations/{peerId} — message history for a peer.
  /// Returns a bare list or a {messages: [...]} envelope.
  Future<List<dynamic>> getConversationHistory(String peerId) async {
    final resp =
        await _dio.get<dynamic>('/api/v1/conversations/$peerId');
    final data = resp.data;
    if (data is List) return data;
    if (data is Map && data['messages'] is List) {
      return data['messages'] as List<dynamic>;
    }
    return [];
  }

  /// GET /api/v1/household/agents — list all agents with heartbeat data.
  Future<List<AgentHeartbeat>> getHouseholdAgents() async {
    final resp =
        await _dio.get<Map<String, dynamic>>('/api/v1/household/agents');
    final agents = resp.data?['agents'] as List<dynamic>? ?? [];
    return agents
        .map((e) => AgentHeartbeat.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// GET /api/board (dashboard port 7778) — coordination board snapshot.
  ///
  /// Returns null when the dashboard service is unreachable.
  Future<CoordBoardData?> getCoordBoard() async {
    try {
      final resp = await _dashDio.get<Map<String, dynamic>>('/api/board');
      if (resp.data == null) return null;
      return CoordBoardData.fromJson(resp.data!);
    } catch (_) {
      return null;
    }
  }
}

/// Heartbeat snapshot for a single agent, sourced from
/// ~/.skcapstone/heartbeats/{name}.json.
class AgentHeartbeat {
  const AgentHeartbeat({
    required this.name,
    required this.status,
    required this.online,
    this.hostname = '',
    this.soulActive = '',
    this.consciousnessActive = false,
    this.loadedModel = '',
    this.timestamp,
  });

  final String name;

  /// "alive" | "busy" | "draining" | "offline"
  final String status;

  /// True when status == "alive" or "busy" and heartbeat is fresh.
  final bool online;
  final String hostname;
  final String soulActive;
  final bool consciousnessActive;
  final String loadedModel;
  final DateTime? timestamp;

  factory AgentHeartbeat.fromJson(Map<String, dynamic> json) {
    final status = json['status'] as String? ?? 'offline';
    final ts = json['timestamp'] as String?;
    DateTime? parsed;
    if (ts != null) {
      parsed = DateTime.tryParse(ts);
    }
    // Consider online if status is alive/busy AND heartbeat is < 10 min old.
    final fresh = parsed != null &&
        DateTime.now().toUtc().difference(parsed).inMinutes < 10;
    final online = fresh && (status == 'alive' || status == 'busy');

    return AgentHeartbeat(
      name: json['agent_name'] as String? ?? '',
      status: status,
      online: online,
      hostname: json['hostname'] as String? ?? '',
      soulActive: json['soul_active'] as String? ?? '',
      consciousnessActive: json['consciousness_active'] as bool? ?? false,
      loadedModel: json['loaded_model'] as String? ?? '',
      timestamp: parsed,
    );
  }
}

/// Singleton SKCapstoneClient provider.
final skCapstoneClientProvider = Provider<SKCapstoneClient>((ref) {
  return SKCapstoneClient();
});

// ── Coordination board models ──────────────────────────────────────────────

/// A single task on the coordination board.
class CoordTask {
  const CoordTask({
    required this.id,
    required this.title,
    required this.priority,
    required this.status,
    this.claimedBy,
    this.tags = const [],
    this.description,
  });

  final String id;
  final String title;

  /// 'critical' | 'high' | 'medium' | 'low'
  final String priority;

  /// 'open' | 'claimed' | 'in_progress' | 'review' | 'done' | 'blocked'
  final String status;

  final String? claimedBy;
  final List<String> tags;
  final String? description;

  bool get isDone => status == 'done';
  bool get isActive =>
      status == 'in_progress' || status == 'claimed' || status == 'review';

  factory CoordTask.fromJson(Map<String, dynamic> json) {
    final rawTags = json['tags'] as List<dynamic>? ?? [];
    return CoordTask(
      id: json['id'] as String? ?? '',
      title: json['title'] as String? ?? json['subject'] as String? ?? '',
      priority: json['priority'] as String? ?? 'medium',
      status: json['status'] as String? ?? 'open',
      claimedBy: json['claimed_by'] as String?,
      tags: rawTags.cast<String>(),
      description: json['description'] as String?,
    );
  }
}

/// Lightweight agent status entry from the board snapshot.
class AgentBoardStatus {
  const AgentBoardStatus({
    required this.name,
    required this.state,
    this.currentTask,
  });

  final String name;

  /// 'active' | 'idle' | 'offline'
  final String state;

  final String? currentTask;

  factory AgentBoardStatus.fromJson(Map<String, dynamic> json) {
    return AgentBoardStatus(
      name: json['name'] as String? ?? '',
      state: json['state'] as String? ?? 'offline',
      currentTask: json['current_task'] as String?,
    );
  }
}

/// Board summary counts.
class CoordSummary {
  const CoordSummary({
    required this.total,
    required this.done,
    required this.open,
    required this.inProgress,
  });

  final int total;
  final int done;
  final int open;
  final int inProgress;

  factory CoordSummary.fromJson(Map<String, dynamic> json) {
    return CoordSummary(
      total: json['total'] as int? ?? 0,
      done: json['done'] as int? ?? 0,
      open: json['open'] as int? ?? 0,
      inProgress: json['in_progress'] as int? ?? 0,
    );
  }
}

/// Full snapshot from GET /api/board.
class CoordBoardData {
  const CoordBoardData({
    required this.tasks,
    required this.agents,
    required this.summary,
  });

  final List<CoordTask> tasks;
  final List<AgentBoardStatus> agents;
  final CoordSummary summary;

  factory CoordBoardData.fromJson(Map<String, dynamic> json) {
    final rawTasks = json['tasks'] as List<dynamic>? ?? [];
    final rawAgents = json['agents'] as List<dynamic>? ?? [];
    final rawSummary = json['summary'] as Map<String, dynamic>? ?? {};

    return CoordBoardData(
      tasks: rawTasks
          .map((e) => CoordTask.fromJson(e as Map<String, dynamic>))
          .toList(),
      agents: rawAgents
          .map((e) => AgentBoardStatus.fromJson(e as Map<String, dynamic>))
          .toList(),
      summary: CoordSummary.fromJson(rawSummary),
    );
  }
}
