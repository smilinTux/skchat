import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../transport/skcomm_client.dart';

/// Represents a known agent from the SKComm daemon.
class AgentInfo {
  final String name;
  final String? fingerprint;
  final String? state;
  final String? host;
  final String? currentTask;

  const AgentInfo({
    required this.name,
    this.fingerprint,
    this.state,
    this.host,
    this.currentTask,
  });

  factory AgentInfo.fromJson(Map<String, dynamic> json) {
    return AgentInfo(
      name: json['name'] as String? ??
          json['agent'] as String? ??
          json['id'] as String? ??
          '',
      fingerprint: json['fingerprint'] as String?,
      state: json['state'] as String?,
      host: json['host'] as String?,
      currentTask: json['current_task'] as String?,
    );
  }
}

/// Notifier that fetches the list of known agents from the SKComm daemon.
class AgentNotifier extends AsyncNotifier<List<AgentInfo>> {
  @override
  Future<List<AgentInfo>> build() => _fetch();

  Future<List<AgentInfo>> _fetch() async {
    final client = ref.read(skcommClientProvider);
    try {
      final raw = await client.getAgents();
      return raw.map(AgentInfo.fromJson).toList();
    } catch (_) {
      // Daemon offline — return empty list.
      return [];
    }
  }

  /// Re-fetch from daemon.
  Future<void> refresh() async {
    state = const AsyncLoading();
    state = await AsyncValue.guard(_fetch);
  }
}

/// Provider for the list of known agents.
/// Returns an empty list when the daemon is unreachable.
final agentProvider =
    AsyncNotifierProvider<AgentNotifier, List<AgentInfo>>(AgentNotifier.new);
