import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../transport/skcomm_client.dart';

/// A single memory entry from the SKComm daemon.
class MemoryEntry {
  final String id;
  final String content;
  final List<String> tags;
  final String? scope;
  final DateTime? createdAt;

  const MemoryEntry({
    required this.id,
    required this.content,
    this.tags = const [],
    this.scope,
    this.createdAt,
  });

  factory MemoryEntry.fromJson(Map<String, dynamic> json) {
    return MemoryEntry(
      id: json['id'] as String? ?? '',
      content: json['content'] as String? ?? '',
      tags: (json['tags'] as List<dynamic>?)?.cast<String>() ?? [],
      scope: json['scope'] as String?,
      createdAt: json['created_at'] != null
          ? DateTime.tryParse(json['created_at'] as String)
          : null,
    );
  }
}

/// Query params for the memory search provider.
class MemoryQuery {
  final String? query;
  const MemoryQuery({this.query});

  @override
  bool operator ==(Object other) =>
      other is MemoryQuery && other.query == query;

  @override
  int get hashCode => query.hashCode;
}

/// Provider that fetches/searches memory entries from the SKComm daemon.
/// Returns an empty list when the daemon is unreachable.
final memoryProvider =
    FutureProvider.family<List<MemoryEntry>, MemoryQuery>((ref, params) async {
  final client = ref.read(skcommClientProvider);
  try {
    final raw = await client.getMemoryEntries(query: params.query);
    return raw.map(MemoryEntry.fromJson).toList();
  } catch (_) {
    return [];
  }
});

/// Notifier for mutating memory (store, refresh).
class MemoryNotifier extends AsyncNotifier<List<MemoryEntry>> {
  @override
  Future<List<MemoryEntry>> build() => _fetchAll();

  Future<List<MemoryEntry>> _fetchAll() async {
    final client = ref.read(skcommClientProvider);
    try {
      final raw = await client.getMemoryEntries();
      return raw.map(MemoryEntry.fromJson).toList();
    } catch (_) {
      return [];
    }
  }

  /// Store a new memory entry and refresh the list.
  Future<void> store({
    required String content,
    List<String>? tags,
    String? scope,
  }) async {
    final client = ref.read(skcommClientProvider);
    try {
      await client.storeMemory(content: content, tags: tags, scope: scope);
      state = const AsyncLoading();
      state = await AsyncValue.guard(_fetchAll);
    } catch (_) {
      // Ignore — daemon offline.
    }
  }

  /// Re-fetch from daemon.
  Future<void> refresh() async {
    state = const AsyncLoading();
    state = await AsyncValue.guard(_fetchAll);
  }
}

/// Writable provider for memory management (list + store).
final memoryNotifierProvider =
    AsyncNotifierProvider<MemoryNotifier, List<MemoryEntry>>(
  MemoryNotifier.new,
);
