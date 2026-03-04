import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../transport/skcomm_client.dart';

/// A chat group.
class ChatGroup {
  final String id;
  final String name;
  final String? description;
  final List<String> members;

  const ChatGroup({
    required this.id,
    required this.name,
    this.description,
    this.members = const [],
  });

  factory ChatGroup.fromJson(Map<String, dynamic> json) {
    return ChatGroup(
      id: json['id'] as String? ?? json['group_id'] as String? ?? '',
      name: json['name'] as String? ?? json['group_name'] as String? ?? '',
      description: json['description'] as String?,
      members: (json['members'] as List<dynamic>?)?.cast<String>() ?? [],
    );
  }
}

/// Well-known fallback group for the skworld team.
const _kSkWorldTeam = ChatGroup(
  id: 'skworld-team',
  name: 'SKWorld Team',
  description: 'Core agent team',
  members: ['opus', 'lumina', 'claude', 'chef'],
);

/// Notifier that fetches groups from the SKComm daemon.
/// Pre-populates with skworld-team when the API is unreachable or empty.
class GroupNotifier extends AsyncNotifier<List<ChatGroup>> {
  @override
  Future<List<ChatGroup>> build() => _fetch();

  Future<List<ChatGroup>> _fetch() async {
    final client = ref.read(skcommClientProvider);
    try {
      final raw = await client.getGroups();
      final groups = raw.map(ChatGroup.fromJson).toList();
      if (groups.isEmpty) {
        return [_kSkWorldTeam];
      }
      final hasSkWorldTeam = groups.any((g) => g.id == 'skworld-team');
      if (!hasSkWorldTeam) {
        return [_kSkWorldTeam, ...groups];
      }
      return groups;
    } catch (_) {
      return [_kSkWorldTeam];
    }
  }

  Future<void> refresh() async {
    state = const AsyncLoading();
    state = await AsyncValue.guard(_fetch);
  }
}

/// Provider for the list of groups.
/// Always includes skworld-team even when the daemon is unreachable.
final groupsProvider =
    AsyncNotifierProvider<GroupNotifier, List<ChatGroup>>(GroupNotifier.new);
