import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import '../../core/router/app_router.dart';
import '../../core/theme/theme.dart';
import '../../models/conversation.dart';
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
        onPressed: () => _showCreateGroupDialog(context, ref),
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

  void _showCreateGroupDialog(BuildContext context, WidgetRef ref) {
    final nameController = TextEditingController();
    final descController = TextEditingController();

    showDialog(
      context: context,
      builder: (dialogContext) => AlertDialog(
        backgroundColor: SovereignColors.surfaceRaised,
        title: const Text('Create Group'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              controller: nameController,
              autofocus: true,
              decoration: const InputDecoration(
                labelText: 'Group name',
                hintText: 'e.g. Penguin Kingdom',
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: descController,
              decoration: const InputDecoration(
                labelText: 'Description (optional)',
                hintText: 'What is this group about?',
              ),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dialogContext).pop(),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () {
              final name = nameController.text.trim();
              if (name.isEmpty) return;

              final groupId =
                  'group-${DateTime.now().millisecondsSinceEpoch}';
              final newGroup = Conversation(
                peerId: groupId,
                displayName: name,
                lastMessage: 'Group created',
                lastMessageTime: DateTime.now(),
                isGroup: true,
                memberCount: 1,
                lastDeliveryStatus: 'delivered',
              );

              ref.read(groupsProvider.notifier).addGroup(newGroup);
              Navigator.of(dialogContext).pop();
            },
            child: const Text('Create'),
          ),
        ],
      ),
    );
  }
}
