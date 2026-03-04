import 'dart:async';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../services/skcapstone_client.dart';

// ── Status enum ───────────────────────────────────────────────────────────────

/// The three observable states of the consciousness loop.
enum ConsciousnessStatus {
  active,
  idle,
  offline;

  static ConsciousnessStatus fromString(String? s) {
    switch (s?.toLowerCase()) {
      case 'active':
        return ConsciousnessStatus.active;
      case 'idle':
        return ConsciousnessStatus.idle;
      default:
        return ConsciousnessStatus.offline;
    }
  }
}

// ── Data models ───────────────────────────────────────────────────────────────

/// Online/offline status for a single LLM backend.
class BackendStatus {
  const BackendStatus({required this.name, required this.online});

  final String name;
  final bool online;
}

/// Full snapshot returned by GET /consciousness.
class ConsciousnessState {
  const ConsciousnessState({
    required this.status,
    required this.messagesProcessed,
    required this.backends,
    required this.lastUpdated,
  });

  final ConsciousnessStatus status;
  final int messagesProcessed;
  final List<BackendStatus> backends;
  final DateTime lastUpdated;

  /// All-offline placeholder — used when the daemon is unreachable.
  factory ConsciousnessState.offline() => ConsciousnessState(
        status: ConsciousnessStatus.offline,
        messagesProcessed: 0,
        backends: _kKnownBackends
            .map((name) => BackendStatus(name: name, online: false))
            .toList(),
        lastUpdated: DateTime.now(),
      );

  /// Parses the JSON envelope from GET /consciousness.
  ///
  /// Handles two backend formats:
  ///   `"ollama": true` (simple bool)
  ///   `"ollama": {"online": true, ...}` (object with online key)
  factory ConsciousnessState.fromJson(Map<String, dynamic> json) {
    final raw = json['backends'];
    final backendsMap = raw is Map<String, dynamic> ? raw : <String, dynamic>{};

    final backends = _kKnownBackends.map((name) {
      final entry = backendsMap[name];
      final online = entry is Map
          ? (entry['online'] as bool? ?? false)
          : (entry as bool? ?? false);
      return BackendStatus(name: name, online: online);
    }).toList();

    return ConsciousnessState(
      status: ConsciousnessStatus.fromString(json['status'] as String?),
      messagesProcessed: json['messages_processed'] as int? ?? 0,
      backends: backends,
      lastUpdated: DateTime.now(),
    );
  }
}

/// The ordered list of LLM backends to display.
const _kKnownBackends = [
  'ollama',
  'anthropic',
  'openai',
  'grok',
  'kimi',
  'nvidia',
  'passthrough',
];

// ── Notifier ──────────────────────────────────────────────────────────────────

/// Polls GET /consciousness every 30 seconds and exposes the result as an
/// [AsyncValue<ConsciousnessState>].
///
/// Falls back to [ConsciousnessState.offline] when the endpoint is unreachable.
class ConsciousnessNotifier extends AsyncNotifier<ConsciousnessState> {
  Timer? _timer;

  @override
  Future<ConsciousnessState> build() async {
    ref.onDispose(() => _timer?.cancel());

    // Schedule periodic refresh after the initial fetch.
    _timer = Timer.periodic(const Duration(seconds: 30), (_) => _poll());

    return _fetch();
  }

  Future<ConsciousnessState> _fetch() async {
    final client = ref.read(skCapstoneClientProvider);
    final data = await client.getConsciousness();
    if (data == null) return ConsciousnessState.offline();
    return ConsciousnessState.fromJson(data);
  }

  Future<void> _poll() async {
    state = AsyncData(await _fetch());
  }

  /// Force-refresh outside the 30-second cycle (e.g. pull-to-refresh).
  Future<void> refresh() async {
    state = const AsyncLoading();
    state = AsyncData(await _fetch());
  }
}

/// The singleton consciousness provider.
final consciousnessProvider =
    AsyncNotifierProvider<ConsciousnessNotifier, ConsciousnessState>(
  ConsciousnessNotifier.new,
);
