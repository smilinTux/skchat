import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import '../../core/router/app_router.dart';
import '../../core/theme/theme.dart';
import 'groups_provider.dart';
import 'widgets/group_tile.dart';

/// Groups screen — lists all group conversations sorted by recency.
/// Each group shows member count, last message, and encryption status.
class GroupsScreen extends ConsumerWidget {
  const GroupsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final groups = ref.watch(groupsProvider);
    final tt = Theme.of(context).textTheme;

    return Scaffold(
      backgroundColor: SovereignColors.surfaceBase,
      appBar: _buildAppBar(context, tt),
      body: groups.isEmpty
          ? _buildEmpty(context, tt)
          : _buildList(groups, context),
      floatingActionButton: FloatingActionButton(
        onPressed: () => context.push(AppRoutes.createGroup),
        tooltip: 'Create group',
        child: const Icon(Icons.group_add_rounded),
      ),
    );
  }

  PreferredSizeWidget _buildAppBar(BuildContext context, TextTheme tt) {
    return AppBar(
      backgroundColor: SovereignColors.surfaceBase,
      title: Text('Groups', style: tt.displayLarge?.copyWith(fontSize: 24)),
      actions: [
        IconButton(
          icon: const Icon(Icons.search_rounded),
          tooltip: 'Search groups',
          onPressed: () {},
        ),
        const SizedBox(width: 4),
      ],
    );
  }

  Widget _buildList(
    List groups,
    BuildContext context,
  ) {
    return ListView.builder(
      padding: const EdgeInsets.only(top: 8, bottom: 100),
      itemCount: groups.length,
      itemBuilder: (context, index) {
        final group = groups[index];
        return GroupTile(
          group: group,
          onTap: () => context.push(AppRoutes.conversationPath(group.peerId)),
          onLongPress: () => context.push(AppRoutes.groupInfoPath(group.peerId)),
        );
      },
    );
  }

  Widget _buildEmpty(BuildContext context, TextTheme tt) {
    return Center(
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Icon(
            Icons.group_outlined,
            size: 48,
            color: SovereignColors.textTertiary,
          ),
          const SizedBox(height: 20),
          Text('No groups yet', style: tt.titleLarge),
          const SizedBox(height: 8),
          Text(
            'Create an encrypted group chat.',
            style: tt.bodyMedium?.copyWith(
              color: SovereignColors.textSecondary,
            ),
          ),
        ],
      ),
    );
  }

}
