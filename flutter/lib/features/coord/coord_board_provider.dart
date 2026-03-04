import 'dart:async';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../services/skcapstone_client.dart';

/// Local agent name used to split MY TASKS from TEAM TASKS.
/// Override at build time via --dart-define=MY_AGENT=yourname
const kMyAgentName = String.fromEnvironment('MY_AGENT', defaultValue: 'opus');

// ── Notifier ──────────────────────────────────────────────────────────────────

/// Polls GET /api/board on the skcapstone dashboard (port 7778) every 60 s.
///
/// Exposes [AsyncValue<CoordBoardData?>].  Null means the dashboard is offline.
class CoordBoardNotifier extends AsyncNotifier<CoordBoardData?> {
  Timer? _timer;

  @override
  Future<CoordBoardData?> build() async {
    ref.onDispose(() => _timer?.cancel());
    _timer = Timer.periodic(const Duration(seconds: 60), (_) => _refresh());
    return _fetch();
  }

  Future<CoordBoardData?> _fetch() async {
    final client = ref.read(skCapstoneClientProvider);
    return client.getCoordBoard();
  }

  Future<void> _refresh() async {
    state = AsyncData(await _fetch());
  }

  /// Force-refresh outside the 60-second cycle (e.g. pull-to-refresh).
  Future<void> refresh() async {
    state = const AsyncLoading();
    state = AsyncData(await _fetch());
  }
}

/// Singleton provider for the coordination board.
final coordBoardProvider =
    AsyncNotifierProvider<CoordBoardNotifier, CoordBoardData?>(
  CoordBoardNotifier.new,
);

// ── Derived providers ─────────────────────────────────────────────────────────

/// Tasks claimed by [kMyAgentName] (any status except done).
final myTasksProvider = Provider<List<CoordTask>>((ref) {
  final board = ref.watch(coordBoardProvider).valueOrNull;
  if (board == null) return [];
  return board.tasks
      .where((t) => t.claimedBy == kMyAgentName && !t.isDone)
      .toList();
});

/// All non-done tasks NOT claimed by [kMyAgentName], sorted by priority.
final teamTasksProvider = Provider<List<CoordTask>>((ref) {
  final board = ref.watch(coordBoardProvider).valueOrNull;
  if (board == null) return [];
  final tasks = board.tasks
      .where((t) => t.claimedBy != kMyAgentName && !t.isDone)
      .toList();
  tasks.sort((a, b) => _priorityRank(a.priority) - _priorityRank(b.priority));
  return tasks;
});

int _priorityRank(String p) {
  switch (p) {
    case 'critical':
      return 0;
    case 'high':
      return 1;
    case 'medium':
      return 2;
    case 'low':
      return 3;
    default:
      return 4;
  }
}
