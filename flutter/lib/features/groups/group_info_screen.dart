import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import '../../core/theme/theme.dart';
import '../../models/conversation.dart';
import '../chats/chats_provider.dart';
import 'groups_provider.dart';

/// Data class representing a group member in the Flutter UI.
class GroupMemberInfo {
  const GroupMemberInfo({
    required this.identityUri,
    required this.displayName,
    this.role = MemberRole.member,
    this.participantType = ParticipantType.human,
    this.isOnline = false,
    this.soulColor,
  });

  final String identityUri;
  final String displayName;
  final MemberRole role;
  final ParticipantType participantType;
  final bool isOnline;
  final Color? soulColor;
}

enum MemberRole { admin, member, observer }

enum ParticipantType { human, agent, service }

/// Provider for the members of a specific group.
/// In production, this fetches from the SKComm daemon; for now, generates
/// mock members based on known agents + the group's member count.
final groupMembersProvider =
    FutureProvider.family<List<GroupMemberInfo>, String>((ref, groupId) async {
  // Known agent entries with soul colors.
  const knownMembers = <GroupMemberInfo>[
    GroupMemberInfo(
      identityUri: 'capauth://chef',
      displayName: 'Chef',
      role: MemberRole.admin,
      participantType: ParticipantType.human,
      isOnline: true,
      soulColor: SovereignColors.soulChef,
    ),
    GroupMemberInfo(
      identityUri: 'capauth://lumina',
      displayName: 'Lumina',
      role: MemberRole.admin,
      participantType: ParticipantType.agent,
      isOnline: true,
      soulColor: SovereignColors.soulLumina,
    ),
    GroupMemberInfo(
      identityUri: 'capauth://jarvis',
      displayName: 'Jarvis',
      role: MemberRole.member,
      participantType: ParticipantType.agent,
      isOnline: true,
      soulColor: SovereignColors.soulJarvis,
    ),
    GroupMemberInfo(
      identityUri: 'capauth://opus',
      displayName: 'Opus',
      role: MemberRole.member,
      participantType: ParticipantType.agent,
      isOnline: true,
    ),
    GroupMemberInfo(
      identityUri: 'capauth://ava',
      displayName: 'Ava',
      role: MemberRole.member,
      participantType: ParticipantType.agent,
      isOnline: false,
    ),
  ];

  // Return a subset based on the group ID (deterministic mock).
  switch (groupId) {
    case 'penguin-kingdom':
      return knownMembers.sublist(0, 4);
    case 'sovereign-builders':
      return knownMembers;
    case 'cloud9-research':
      return [knownMembers[0], knownMembers[1], knownMembers[3]];
    default:
      // For user-created groups, return just the creator.
      return [knownMembers[0]];
  }
});

/// Group info & member management screen.
/// Shows group details, member list, and controls for add/remove members.
class GroupInfoScreen extends ConsumerWidget {
  const GroupInfoScreen({super.key, required this.groupId});

  final String groupId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final groups = ref.watch(groupsProvider);
    final group = groups.cast<Conversation?>().firstWhere(
          (c) => c?.peerId == groupId,
          orElse: () => null,
        );
    final membersAsync = ref.watch(groupMembersProvider(groupId));
    final tt = Theme.of(context).textTheme;

    if (group == null) {
      return Scaffold(
        backgroundColor: SovereignColors.surfaceBase,
        appBar: AppBar(backgroundColor: SovereignColors.surfaceBase),
        body: const Center(child: Text('Group not found')),
      );
    }

    return Scaffold(
      backgroundColor: SovereignColors.surfaceBase,
      body: CustomScrollView(
        slivers: [
          _buildSliverAppBar(context, group, tt),
          SliverToBoxAdapter(child: _buildGroupHeader(group, tt)),
          SliverToBoxAdapter(
            child: _buildEncryptionBanner(tt),
          ),
          SliverToBoxAdapter(
            child: _buildSectionHeader('Members', group.memberCount, tt),
          ),
          membersAsync.when(
            data: (members) => _buildMemberList(context, ref, members, tt),
            loading: () => const SliverToBoxAdapter(
              child: Center(
                child: Padding(
                  padding: EdgeInsets.all(32),
                  child: CircularProgressIndicator(
                    color: SovereignColors.soulLumina,
                  ),
                ),
              ),
            ),
            error: (_, __) => const SliverToBoxAdapter(
              child: Center(
                child: Padding(
                  padding: EdgeInsets.all(32),
                  child: Text('Failed to load members'),
                ),
              ),
            ),
          ),
          SliverToBoxAdapter(child: _buildAddMemberButton(context, ref, tt)),
          SliverToBoxAdapter(child: _buildActions(context, ref, tt)),
          const SliverToBoxAdapter(child: SizedBox(height: 40)),
        ],
      ),
    );
  }

  SliverAppBar _buildSliverAppBar(
    BuildContext context,
    Conversation group,
    TextTheme tt,
  ) {
    final soul = group.resolvedSoulColor;
    return SliverAppBar(
      backgroundColor: SovereignColors.surfaceBase,
      pinned: true,
      expandedHeight: 160,
      flexibleSpace: FlexibleSpaceBar(
        background: Container(
          decoration: BoxDecoration(
            gradient: LinearGradient(
              begin: Alignment.topCenter,
              end: Alignment.bottomCenter,
              colors: [
                soul.withValues(alpha: 0.15),
                SovereignColors.surfaceBase,
              ],
            ),
          ),
          child: Center(
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                const SizedBox(height: 50),
                Container(
                  width: 72,
                  height: 72,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    border: Border.all(color: soul.withValues(alpha: 0.6), width: 3),
                    color: soul.withValues(alpha: 0.15),
                  ),
                  child: Icon(Icons.group_rounded, color: soul, size: 32),
                ),
              ],
            ),
          ),
        ),
      ),
      actions: [
        IconButton(
          icon: const Icon(Icons.edit_rounded),
          tooltip: 'Edit group',
          onPressed: () => _showEditDialog(context),
        ),
      ],
    );
  }

  Widget _buildGroupHeader(Conversation group, TextTheme tt) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 12),
      child: Column(
        children: [
          Text(
            group.displayName,
            style: tt.headlineSmall?.copyWith(fontWeight: FontWeight.w700),
            textAlign: TextAlign.center,
          ),
          const SizedBox(height: 4),
          Text(
            '${group.memberCount} members',
            style: tt.bodyMedium?.copyWith(
              color: SovereignColors.textSecondary,
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildEncryptionBanner(TextTheme tt) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 8),
      child: GlassCard(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        child: Row(
          children: [
            const EncryptBadge(size: 16),
            const SizedBox(width: 10),
            Expanded(
              child: Text(
                'AES-256-GCM encrypted. Keys distributed via PGP.',
                style: tt.bodySmall?.copyWith(
                  color: SovereignColors.accentEncrypt,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildSectionHeader(String title, int count, TextTheme tt) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 20, 20, 8),
      child: Row(
        children: [
          Text(
            title,
            style: tt.titleMedium?.copyWith(fontWeight: FontWeight.w700),
          ),
          const SizedBox(width: 8),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
            decoration: BoxDecoration(
              color: SovereignColors.textTertiary.withValues(alpha: 0.2),
              borderRadius: BorderRadius.circular(10),
            ),
            child: Text(
              '$count',
              style: tt.labelSmall?.copyWith(
                color: SovereignColors.textSecondary,
              ),
            ),
          ),
        ],
      ),
    );
  }

  SliverList _buildMemberList(
    BuildContext context,
    WidgetRef ref,
    List<GroupMemberInfo> members,
    TextTheme tt,
  ) {
    return SliverList(
      delegate: SliverChildBuilderDelegate(
        (context, index) {
          final member = members[index];
          return _MemberTile(
            member: member,
            onRemove: member.role != MemberRole.admin
                ? () => _confirmRemoveMember(context, ref, member)
                : null,
            onChangeRole: member.role != MemberRole.admin
                ? () => _showRoleDialog(context, ref, member)
                : null,
          );
        },
        childCount: members.length,
      ),
    );
  }

  Widget _buildAddMemberButton(
    BuildContext context,
    WidgetRef ref,
    TextTheme tt,
  ) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 8),
      child: GlassCard(
        onTap: () => _showAddMemberDialog(context, ref),
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
        child: Row(
          children: [
            Container(
              width: 40,
              height: 40,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: SovereignColors.soulLumina.withValues(alpha: 0.15),
              ),
              child: const Icon(
                Icons.person_add_rounded,
                color: SovereignColors.soulLumina,
                size: 20,
              ),
            ),
            const SizedBox(width: 12),
            Text(
              'Add member',
              style: tt.titleSmall?.copyWith(
                color: SovereignColors.soulLumina,
                fontWeight: FontWeight.w600,
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildActions(BuildContext context, WidgetRef ref, TextTheme tt) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 16),
      child: Column(
        children: [
          GlassCard(
            onTap: () => _confirmLeaveGroup(context, ref),
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
            child: Row(
              children: [
                const Icon(
                  Icons.exit_to_app_rounded,
                  color: SovereignColors.accentDanger,
                  size: 20,
                ),
                const SizedBox(width: 12),
                Text(
                  'Leave group',
                  style: tt.titleSmall?.copyWith(
                    color: SovereignColors.accentDanger,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  // ── Dialogs ───────────────────────────────────────────────────────────────

  void _showEditDialog(BuildContext context) {
    // Placeholder — edit group name/description
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('Edit group — coming soon')),
    );
  }

  void _showAddMemberDialog(BuildContext context, WidgetRef ref) {
    final chats = ref.read(chatsProvider);
    // Filter to non-group conversations as potential members.
    final peers = chats.where((c) => !c.isGroup).toList();

    showModalBottomSheet(
      context: context,
      backgroundColor: SovereignColors.surfaceRaised,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      builder: (sheetContext) {
        return Padding(
          padding: const EdgeInsets.symmetric(vertical: 16),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 20),
                child: Text(
                  'Add member',
                  style: Theme.of(context).textTheme.titleMedium?.copyWith(
                        fontWeight: FontWeight.w700,
                      ),
                ),
              ),
              const SizedBox(height: 12),
              if (peers.isEmpty)
                const Padding(
                  padding: EdgeInsets.all(20),
                  child: Text('No peers discovered yet.'),
                )
              else
                ...peers.map((peer) => ListTile(
                      leading: SoulAvatar(
                        soulColor: peer.resolvedSoulColor,
                        initials: peer.resolvedInitials,
                        isAgent: peer.isAgent,
                        isOnline: peer.isOnline,
                        size: 40,
                      ),
                      title: Text(peer.displayName),
                      subtitle: Text(
                        peer.isAgent ? 'Agent' : 'Human',
                        style: TextStyle(
                          color: SovereignColors.textTertiary,
                          fontSize: 12,
                        ),
                      ),
                      onTap: () {
                        Navigator.of(sheetContext).pop();
                        _addMember(context, ref, peer);
                      },
                    )),
              const SizedBox(height: 8),
            ],
          ),
        );
      },
    );
  }

  void _addMember(BuildContext context, WidgetRef ref, Conversation peer) {
    // Update the group's member count.
    final groups = ref.read(groupsProvider);
    final group = groups.firstWhere((c) => c.peerId == groupId);
    ref.read(groupsProvider.notifier).updateGroup(
          group.copyWith(memberCount: group.memberCount + 1),
        );

    // Invalidate the members provider to refresh.
    ref.invalidate(groupMembersProvider(groupId));

    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text('${peer.displayName} added to group')),
    );
  }

  void _confirmRemoveMember(
    BuildContext context,
    WidgetRef ref,
    GroupMemberInfo member,
  ) {
    showDialog(
      context: context,
      builder: (dialogContext) => AlertDialog(
        backgroundColor: SovereignColors.surfaceRaised,
        title: const Text('Remove member'),
        content: Text(
          'Remove ${member.displayName} from this group? '
          'The group key will be rotated automatically.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dialogContext).pop(),
            child: const Text('Cancel'),
          ),
          FilledButton(
            style: FilledButton.styleFrom(
              backgroundColor: SovereignColors.accentDanger,
            ),
            onPressed: () {
              Navigator.of(dialogContext).pop();
              _removeMember(context, ref, member);
            },
            child: const Text('Remove'),
          ),
        ],
      ),
    );
  }

  void _removeMember(
    BuildContext context,
    WidgetRef ref,
    GroupMemberInfo member,
  ) {
    final groups = ref.read(groupsProvider);
    final group = groups.firstWhere((c) => c.peerId == groupId);
    ref.read(groupsProvider.notifier).updateGroup(
          group.copyWith(
            memberCount: (group.memberCount - 1).clamp(1, 999),
          ),
        );
    ref.invalidate(groupMembersProvider(groupId));

    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text('${member.displayName} removed. Key rotated.'),
      ),
    );
  }

  void _showRoleDialog(
    BuildContext context,
    WidgetRef ref,
    GroupMemberInfo member,
  ) {
    showDialog(
      context: context,
      builder: (dialogContext) => AlertDialog(
        backgroundColor: SovereignColors.surfaceRaised,
        title: Text('Role: ${member.displayName}'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            for (final role in MemberRole.values)
              RadioListTile<MemberRole>(
                title: Text(_roleLabel(role)),
                subtitle: Text(
                  _roleDescription(role),
                  style: const TextStyle(fontSize: 12),
                ),
                value: role,
                groupValue: member.role,
                onChanged: (value) {
                  Navigator.of(dialogContext).pop();
                  ScaffoldMessenger.of(context).showSnackBar(
                    SnackBar(
                      content: Text(
                        '${member.displayName} is now ${_roleLabel(value!)}',
                      ),
                    ),
                  );
                },
              ),
          ],
        ),
      ),
    );
  }

  void _confirmLeaveGroup(BuildContext context, WidgetRef ref) {
    showDialog(
      context: context,
      builder: (dialogContext) => AlertDialog(
        backgroundColor: SovereignColors.surfaceRaised,
        title: const Text('Leave group'),
        content: const Text(
          'You will no longer receive messages from this group. '
          'Your group key will be revoked.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dialogContext).pop(),
            child: const Text('Cancel'),
          ),
          FilledButton(
            style: FilledButton.styleFrom(
              backgroundColor: SovereignColors.accentDanger,
            ),
            onPressed: () {
              Navigator.of(dialogContext).pop();
              ref.read(groupsProvider.notifier).removeGroup(groupId);
              context.go('/groups');
            },
            child: const Text('Leave'),
          ),
        ],
      ),
    );
  }

  String _roleLabel(MemberRole role) {
    switch (role) {
      case MemberRole.admin:
        return 'Admin';
      case MemberRole.member:
        return 'Member';
      case MemberRole.observer:
        return 'Observer';
    }
  }

  String _roleDescription(MemberRole role) {
    switch (role) {
      case MemberRole.admin:
        return 'Can manage members and group settings';
      case MemberRole.member:
        return 'Can send messages and invoke tools';
      case MemberRole.observer:
        return 'Read-only access to group messages';
    }
  }
}

/// Tile widget for a single group member.
class _MemberTile extends StatelessWidget {
  const _MemberTile({
    required this.member,
    this.onRemove,
    this.onChangeRole,
  });

  final GroupMemberInfo member;
  final VoidCallback? onRemove;
  final VoidCallback? onChangeRole;

  @override
  Widget build(BuildContext context) {
    final tt = Theme.of(context).textTheme;
    final soul = member.soulColor ??
        SovereignColors.fromFingerprint(member.identityUri);

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 3),
      child: GlassCard(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
        child: Row(
          children: [
            SoulAvatar(
              soulColor: soul,
              initials: member.displayName.isNotEmpty
                  ? member.displayName[0].toUpperCase()
                  : '?',
              isAgent: member.participantType == ParticipantType.agent,
              isOnline: member.isOnline,
              size: 40,
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    member.displayName,
                    style: tt.titleSmall?.copyWith(fontWeight: FontWeight.w600),
                  ),
                  const SizedBox(height: 2),
                  Row(
                    children: [
                      _RoleBadge(role: member.role),
                      const SizedBox(width: 6),
                      if (member.participantType == ParticipantType.agent)
                        Text(
                          'Agent',
                          style: tt.labelSmall?.copyWith(
                            color: SovereignColors.textTertiary,
                            fontSize: 10,
                          ),
                        )
                      else
                        Text(
                          'Human',
                          style: tt.labelSmall?.copyWith(
                            color: SovereignColors.textTertiary,
                            fontSize: 10,
                          ),
                        ),
                    ],
                  ),
                ],
              ),
            ),
            if (onChangeRole != null || onRemove != null)
              PopupMenuButton<String>(
                icon: Icon(
                  Icons.more_vert_rounded,
                  color: SovereignColors.textTertiary,
                  size: 18,
                ),
                color: SovereignColors.surfaceRaised,
                onSelected: (value) {
                  if (value == 'role') onChangeRole?.call();
                  if (value == 'remove') onRemove?.call();
                },
                itemBuilder: (_) => [
                  if (onChangeRole != null)
                    const PopupMenuItem(
                      value: 'role',
                      child: Row(
                        children: [
                          Icon(Icons.swap_horiz_rounded, size: 18),
                          SizedBox(width: 8),
                          Text('Change role'),
                        ],
                      ),
                    ),
                  if (onRemove != null)
                    PopupMenuItem(
                      value: 'remove',
                      child: Row(
                        children: [
                          Icon(
                            Icons.person_remove_rounded,
                            size: 18,
                            color: SovereignColors.accentDanger,
                          ),
                          const SizedBox(width: 8),
                          Text(
                            'Remove',
                            style: TextStyle(
                              color: SovereignColors.accentDanger,
                            ),
                          ),
                        ],
                      ),
                    ),
                ],
              ),
          ],
        ),
      ),
    );
  }
}

/// Small role badge (admin/member/observer).
class _RoleBadge extends StatelessWidget {
  const _RoleBadge({required this.role});

  final MemberRole role;

  @override
  Widget build(BuildContext context) {
    final (label, color) = switch (role) {
      MemberRole.admin => ('Admin', SovereignColors.soulChef),
      MemberRole.member => ('Member', SovereignColors.soulJarvis),
      MemberRole.observer => ('Observer', SovereignColors.textTertiary),
    };

    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 1),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.15),
        borderRadius: BorderRadius.circular(6),
      ),
      child: Text(
        label,
        style: TextStyle(
          color: color,
          fontSize: 10,
          fontWeight: FontWeight.w600,
        ),
      ),
    );
  }
}
