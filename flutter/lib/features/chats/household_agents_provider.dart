import 'dart:async';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../services/skcapstone_client.dart';

/// Polls GET /api/v1/household/agents on the skcapstone daemon (port 7777)
/// every 30 seconds and exposes the result as an
/// [AsyncValue<List<AgentHeartbeat>>].
///
/// Falls back to an empty list when the daemon is unreachable so callers
/// can show an "offline" state without crashing.
class HouseholdAgentsNotifier
    extends AsyncNotifier<List<AgentHeartbeat>> {
  Timer? _timer;

  @override
  Future<List<AgentHeartbeat>> build() async {
    ref.onDispose(() => _timer?.cancel());
    _timer =
        Timer.periodic(const Duration(seconds: 30), (_) => _refresh());
    return _fetch();
  }

  Future<List<AgentHeartbeat>> _fetch() async {
    try {
      final client = ref.read(skCapstoneClientProvider);
      return await client.getHouseholdAgents();
    } catch (_) {
      return [];
    }
  }

  Future<void> _refresh() async {
    state = AsyncData(await _fetch());
  }

  /// Force-refresh outside the 30-second cycle (e.g. pull-to-refresh).
  Future<void> refresh() async {
    state = const AsyncLoading();
    state = AsyncData(await _fetch());
  }
}

final householdAgentsProvider =
    AsyncNotifierProvider<HouseholdAgentsNotifier, List<AgentHeartbeat>>(
  HouseholdAgentsNotifier.new,
);
