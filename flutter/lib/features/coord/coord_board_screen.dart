import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/theme/glass_widgets.dart';
import '../../core/theme/sovereign_colors.dart';
import '../../services/skcapstone_client.dart';
import 'coord_board_provider.dart';

/// Team Coordination Board screen.
///
/// Shows MY TASKS (tasks claimed by the local agent) and TEAM TASKS
/// (all open/active tasks not claimed by the local agent).
/// Pull-to-refresh.  Tap a task for its full detail sheet.
class CoordBoardScreen extends ConsumerWidget {
  const CoordBoardScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final asyncBoard = ref.watch(coordBoardProvider);
    final myTasks = ref.watch(myTasksProvider);
    final teamTasks = ref.watch(teamTasksProvider);
    final tt = Theme.of(context).textTheme;

    return Scaffold(
      backgroundColor: SovereignColors.surfaceBase,
      appBar: _buildAppBar(context, ref, tt, asyncBoard),
      body: asyncBoard.when(
        loading: () => const Center(
          child: CircularProgressIndicator(color: SovereignColors.soulLumina),
        ),
        error: (_, __) => _buildOffline(context, ref, tt),
        data: (board) => board == null
            ? _buildOffline(context, ref, tt)
            : _buildBoard(context, ref, tt, board, myTasks, teamTasks),
      ),
    );
  }

  PreferredSizeWidget _buildAppBar(
    BuildContext context,
    WidgetRef ref,
    TextTheme tt,
    AsyncValue<CoordBoardData?> asyncBoard,
  ) {
    final summary = asyncBoard.valueOrNull?.summary;

    return AppBar(
      backgroundColor: SovereignColors.surfaceBase,
      title: Row(
        children: [
          Text('Team Board', style: tt.displayLarge?.copyWith(fontSize: 24)),
          if (summary != null) ...[
            const SizedBox(width: 10),
            _SummaryChip(
              label: '${summary.inProgress} active',
              color: SovereignColors.soulLumina,
            ),
          ],
        ],
      ),
      actions: [
        IconButton(
          icon: const Icon(
            Icons.refresh_rounded,
            color: SovereignColors.textSecondary,
          ),
          tooltip: 'Refresh',
          onPressed: () =>
              ref.read(coordBoardProvider.notifier).refresh(),
        ),
        const SizedBox(width: 4),
      ],
    );
  }

  Widget _buildOffline(
    BuildContext context,
    WidgetRef ref,
    TextTheme tt,
  ) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            const Icon(
              Icons.cloud_off_outlined,
              size: 52,
              color: SovereignColors.textTertiary,
            ),
            const SizedBox(height: 16),
            Text(
              'Dashboard offline',
              style: tt.titleMedium?.copyWith(
                color: SovereignColors.textSecondary,
              ),
            ),
            const SizedBox(height: 8),
            Text(
              'Could not reach skcapstone dashboard (port 7778).',
              style: tt.bodyMedium?.copyWith(
                color: SovereignColors.textTertiary,
              ),
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 24),
            OutlinedButton.icon(
              onPressed: () =>
                  ref.read(coordBoardProvider.notifier).refresh(),
              icon: const Icon(Icons.refresh_rounded, size: 16),
              label: const Text('Retry'),
              style: OutlinedButton.styleFrom(
                foregroundColor: SovereignColors.soulLumina,
                side: const BorderSide(
                  color: SovereignColors.soulLumina,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildBoard(
    BuildContext context,
    WidgetRef ref,
    TextTheme tt,
    CoordBoardData board,
    List<CoordTask> myTasks,
    List<CoordTask> teamTasks,
  ) {
    return RefreshIndicator(
      color: SovereignColors.soulLumina,
      backgroundColor: SovereignColors.surfaceRaised,
      onRefresh: () => ref.read(coordBoardProvider.notifier).refresh(),
      child: CustomScrollView(
        physics: const AlwaysScrollableScrollPhysics(
          parent: BouncingScrollPhysics(),
        ),
        slivers: [
          // ── Summary row ──────────────────────────────────────────
          SliverToBoxAdapter(
            child: Padding(
              padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
              child: _SummaryRow(summary: board.summary),
            ),
          ),

          // ── MY TASKS ─────────────────────────────────────────────
          SliverToBoxAdapter(
            child: _SectionHeader(
              title: 'MY TASKS',
              subtitle: kMyAgentName,
              count: myTasks.length,
              accentColor: SovereignColors.soulLumina,
            ),
          ),
          if (myTasks.isEmpty)
            const SliverToBoxAdapter(child: _EmptySection(label: 'No active tasks'))
          else
            SliverList(
              delegate: SliverChildBuilderDelegate(
                (context, index) => _TaskTile(
                  task: myTasks[index],
                  onTap: () => _showTaskDetail(context, myTasks[index]),
                ),
                childCount: myTasks.length,
              ),
            ),

          // ── TEAM TASKS ───────────────────────────────────────────
          SliverToBoxAdapter(
            child: _SectionHeader(
              title: 'TEAM TASKS',
              subtitle: 'open & active',
              count: teamTasks.length,
              accentColor: SovereignColors.soulJarvis,
            ),
          ),
          if (teamTasks.isEmpty)
            const SliverToBoxAdapter(child: _EmptySection(label: 'No open team tasks'))
          else
            SliverList(
              delegate: SliverChildBuilderDelegate(
                (context, index) => _TaskTile(
                  task: teamTasks[index],
                  onTap: () => _showTaskDetail(context, teamTasks[index]),
                ),
                childCount: teamTasks.length,
              ),
            ),

          const SliverToBoxAdapter(child: SizedBox(height: 100)),
        ],
      ),
    );
  }

  void _showTaskDetail(BuildContext context, CoordTask task) {
    showModalBottomSheet<void>(
      context: context,
      backgroundColor: SovereignColors.surfaceRaised,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      isScrollControlled: true,
      builder: (_) => _TaskDetailSheet(task: task),
    );
  }
}

// ── Summary row ───────────────────────────────────────────────────────────────

class _SummaryRow extends StatelessWidget {
  const _SummaryRow({required this.summary});

  final CoordSummary summary;

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        _StatCell(
          label: 'Total',
          value: '${summary.total}',
          color: SovereignColors.textSecondary,
        ),
        _StatCell(
          label: 'Active',
          value: '${summary.inProgress}',
          color: SovereignColors.soulLumina,
        ),
        _StatCell(
          label: 'Open',
          value: '${summary.open}',
          color: SovereignColors.accentWarning,
        ),
        _StatCell(
          label: 'Done',
          value: '${summary.done}',
          color: SovereignColors.accentEncrypt,
        ),
      ],
    );
  }
}

class _StatCell extends StatelessWidget {
  const _StatCell({
    required this.label,
    required this.value,
    required this.color,
  });

  final String label;
  final String value;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Expanded(
      child: GlassCard(
        margin: const EdgeInsets.symmetric(horizontal: 4, vertical: 4),
        padding: const EdgeInsets.symmetric(vertical: 10),
        child: Column(
          children: [
            Text(
              value,
              style: TextStyle(
                color: color,
                fontSize: 20,
                fontWeight: FontWeight.w700,
              ),
            ),
            const SizedBox(height: 2),
            Text(
              label,
              style: const TextStyle(
                color: SovereignColors.textTertiary,
                fontSize: 11,
                fontWeight: FontWeight.w500,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// ── Section header ─────────────────────────────────────────────────────────────

class _SectionHeader extends StatelessWidget {
  const _SectionHeader({
    required this.title,
    required this.subtitle,
    required this.count,
    required this.accentColor,
  });

  final String title;
  final String subtitle;
  final int count;
  final Color accentColor;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 16, 16, 6),
      child: Row(
        children: [
          Container(
            width: 3,
            height: 14,
            decoration: BoxDecoration(
              color: accentColor,
              borderRadius: BorderRadius.circular(2),
            ),
          ),
          const SizedBox(width: 8),
          Text(
            title,
            style: const TextStyle(
              color: SovereignColors.textPrimary,
              fontSize: 13,
              fontWeight: FontWeight.w700,
              letterSpacing: 0.8,
            ),
          ),
          const SizedBox(width: 6),
          Text(
            subtitle,
            style: const TextStyle(
              color: SovereignColors.textTertiary,
              fontSize: 12,
            ),
          ),
          const Spacer(),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
            decoration: BoxDecoration(
              color: accentColor.withValues(alpha: 0.15),
              borderRadius: BorderRadius.circular(10),
              border: Border.all(
                color: accentColor.withValues(alpha: 0.3),
                width: 0.5,
              ),
            ),
            child: Text(
              '$count',
              style: TextStyle(
                color: accentColor,
                fontSize: 11,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

// ── Empty section placeholder ──────────────────────────────────────────────────

class _EmptySection extends StatelessWidget {
  const _EmptySection({required this.label});

  final String label;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      child: GlassCard(
        opacity: 0.03,
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
        child: Row(
          children: [
            const Icon(
              Icons.check_circle_outline,
              size: 16,
              color: SovereignColors.textTertiary,
            ),
            const SizedBox(width: 10),
            Text(
              label,
              style: const TextStyle(
                color: SovereignColors.textTertiary,
                fontSize: 13,
                fontStyle: FontStyle.italic,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// ── Task tile ─────────────────────────────────────────────────────────────────

class _TaskTile extends StatelessWidget {
  const _TaskTile({required this.task, required this.onTap});

  final CoordTask task;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final priorityColor = _priorityColor(task.priority);
    final statusColor = _statusColor(task.status);

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 3),
      child: GlassCard(
        onTap: onTap,
        opacity: 0.05,
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 11),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // Priority dot
            Padding(
              padding: const EdgeInsets.only(top: 5),
              child: Container(
                width: 8,
                height: 8,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: priorityColor,
                  boxShadow: [
                    BoxShadow(
                      color: priorityColor.withValues(alpha: 0.5),
                      blurRadius: 4,
                    ),
                  ],
                ),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  // Title
                  Text(
                    task.title,
                    style: const TextStyle(
                      color: SovereignColors.textPrimary,
                      fontSize: 13,
                      fontWeight: FontWeight.w500,
                      height: 1.35,
                    ),
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                  ),
                  const SizedBox(height: 6),
                  // Meta row
                  Row(
                    children: [
                      // ID chip
                      _MetaChip(
                        label: task.id.length > 8
                            ? task.id.substring(0, 8)
                            : task.id,
                        color: SovereignColors.textTertiary,
                        monospace: true,
                      ),
                      const SizedBox(width: 6),
                      // Status chip
                      _MetaChip(
                        label: _statusLabel(task.status),
                        color: statusColor,
                      ),
                      if (task.claimedBy != null) ...[
                        const SizedBox(width: 6),
                        _MetaChip(
                          label: task.claimedBy!,
                          color: SovereignColors.soulJarvis,
                        ),
                      ],
                    ],
                  ),
                ],
              ),
            ),
            const SizedBox(width: 8),
            const Icon(
              Icons.chevron_right,
              size: 16,
              color: SovereignColors.textTertiary,
            ),
          ],
        ),
      ),
    );
  }

  String _statusLabel(String s) {
    switch (s) {
      case 'in_progress':
        return 'in progress';
      case 'claimed':
        return 'claimed';
      case 'review':
        return 'review';
      case 'blocked':
        return 'blocked';
      case 'done':
        return 'done';
      default:
        return 'open';
    }
  }

  Color _priorityColor(String p) {
    switch (p) {
      case 'critical':
        return SovereignColors.accentDanger;
      case 'high':
        return const Color(0xFFFF6B35); // orange
      case 'medium':
        return SovereignColors.accentWarning;
      case 'low':
        return SovereignColors.textTertiary;
      default:
        return SovereignColors.textTertiary;
    }
  }

  Color _statusColor(String s) {
    switch (s) {
      case 'in_progress':
        return SovereignColors.soulLumina;
      case 'claimed':
        return SovereignColors.accentWarning;
      case 'review':
        return SovereignColors.soulJarvis;
      case 'blocked':
        return SovereignColors.accentDanger;
      case 'done':
        return SovereignColors.accentEncrypt;
      default:
        return SovereignColors.textTertiary;
    }
  }
}

class _MetaChip extends StatelessWidget {
  const _MetaChip({
    required this.label,
    required this.color,
    this.monospace = false,
  });

  final String label;
  final Color color;
  final bool monospace;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(4),
        border: Border.all(color: color.withValues(alpha: 0.3), width: 0.5),
      ),
      child: Text(
        label,
        style: TextStyle(
          color: color,
          fontSize: 10,
          fontWeight: FontWeight.w600,
          fontFamily: monospace ? 'JetBrainsMono' : null,
          letterSpacing: monospace ? 0.3 : 0.2,
        ),
      ),
    );
  }
}

// ── Summary chip ──────────────────────────────────────────────────────────────

class _SummaryChip extends StatelessWidget {
  const _SummaryChip({required this.label, required this.color});

  final String label;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.15),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: color.withValues(alpha: 0.35), width: 0.5),
      ),
      child: Text(
        label,
        style: TextStyle(
          color: color,
          fontSize: 11,
          fontWeight: FontWeight.w600,
          letterSpacing: 0.2,
        ),
      ),
    );
  }
}

// ── Task detail bottom sheet ───────────────────────────────────────────────────

class _TaskDetailSheet extends StatelessWidget {
  const _TaskDetailSheet({required this.task});

  final CoordTask task;

  @override
  Widget build(BuildContext context) {
    final priorityColor = _priorityColor(task.priority);
    final statusColor = _statusColor(task.status);
    final tt = Theme.of(context).textTheme;

    return DraggableScrollableSheet(
      initialChildSize: 0.55,
      minChildSize: 0.35,
      maxChildSize: 0.9,
      expand: false,
      builder: (context, scrollController) {
        return Container(
          decoration: const BoxDecoration(
            color: SovereignColors.surfaceRaised,
            borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
          ),
          child: Column(
            children: [
              // Handle bar
              Padding(
                padding: const EdgeInsets.only(top: 12, bottom: 8),
                child: Container(
                  width: 36,
                  height: 4,
                  decoration: BoxDecoration(
                    color: SovereignColors.surfaceGlassBorder,
                    borderRadius: BorderRadius.circular(2),
                  ),
                ),
              ),
              Expanded(
                child: ListView(
                  controller: scrollController,
                  padding: const EdgeInsets.fromLTRB(20, 4, 20, 40),
                  children: [
                    // ID + priority
                    Row(
                      children: [
                        Text(
                          task.id.length > 8
                              ? task.id.substring(0, 8)
                              : task.id,
                          style: const TextStyle(
                            color: SovereignColors.textTertiary,
                            fontSize: 12,
                            fontFamily: 'JetBrainsMono',
                            letterSpacing: 0.4,
                          ),
                        ),
                        const Spacer(),
                        Container(
                          padding: const EdgeInsets.symmetric(
                              horizontal: 8, vertical: 3),
                          decoration: BoxDecoration(
                            color: priorityColor.withValues(alpha: 0.15),
                            borderRadius: BorderRadius.circular(6),
                            border: Border.all(
                              color: priorityColor.withValues(alpha: 0.4),
                              width: 0.5,
                            ),
                          ),
                          child: Text(
                            task.priority.toUpperCase(),
                            style: TextStyle(
                              color: priorityColor,
                              fontSize: 10,
                              fontWeight: FontWeight.w700,
                              letterSpacing: 0.6,
                            ),
                          ),
                        ),
                      ],
                    ),
                    const SizedBox(height: 12),

                    // Title
                    Text(
                      task.title,
                      style: tt.titleLarge?.copyWith(
                        color: SovereignColors.textPrimary,
                        height: 1.35,
                      ),
                    ),
                    const SizedBox(height: 16),

                    // Status row
                    _DetailRow(
                      label: 'Status',
                      child: Container(
                        padding: const EdgeInsets.symmetric(
                            horizontal: 8, vertical: 3),
                        decoration: BoxDecoration(
                          color: statusColor.withValues(alpha: 0.15),
                          borderRadius: BorderRadius.circular(6),
                        ),
                        child: Text(
                          _statusLabel(task.status),
                          style: TextStyle(
                            color: statusColor,
                            fontSize: 12,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ),
                    ),
                    const SizedBox(height: 10),

                    // Assigned
                    if (task.claimedBy != null) ...[
                      _DetailRow(
                        label: 'Assigned',
                        child: Row(
                          children: [
                            const Icon(
                              Icons.smart_toy_outlined,
                              size: 14,
                              color: SovereignColors.textSecondary,
                            ),
                            const SizedBox(width: 6),
                            Text(
                              task.claimedBy!,
                              style: const TextStyle(
                                color: SovereignColors.textPrimary,
                                fontSize: 13,
                                fontWeight: FontWeight.w500,
                              ),
                            ),
                          ],
                        ),
                      ),
                      const SizedBox(height: 10),
                    ],

                    // Tags
                    if (task.tags.isNotEmpty) ...[
                      _DetailRow(
                        label: 'Tags',
                        child: Wrap(
                          spacing: 6,
                          runSpacing: 4,
                          children: task.tags
                              .map((t) => Container(
                                    padding: const EdgeInsets.symmetric(
                                        horizontal: 8, vertical: 3),
                                    decoration: BoxDecoration(
                                      color: SovereignColors.surfaceGlass,
                                      borderRadius: BorderRadius.circular(6),
                                      border: Border.all(
                                        color: SovereignColors.surfaceGlassBorder,
                                      ),
                                    ),
                                    child: Text(
                                      t,
                                      style: const TextStyle(
                                        color: SovereignColors.textSecondary,
                                        fontSize: 11,
                                      ),
                                    ),
                                  ))
                              .toList(),
                        ),
                      ),
                      const SizedBox(height: 10),
                    ],

                    // Description
                    const Divider(
                      color: SovereignColors.surfaceGlassBorder,
                      height: 24,
                    ),
                    const Text(
                      'Description',
                      style: TextStyle(
                        color: SovereignColors.textTertiary,
                        fontSize: 11,
                        fontWeight: FontWeight.w600,
                        letterSpacing: 0.5,
                      ),
                    ),
                    const SizedBox(height: 8),
                    Text(
                      task.description ?? task.title,
                      style: const TextStyle(
                        color: SovereignColors.textSecondary,
                        fontSize: 13,
                        height: 1.5,
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
        );
      },
    );
  }

  String _statusLabel(String s) {
    switch (s) {
      case 'in_progress':
        return 'In Progress';
      case 'claimed':
        return 'Claimed';
      case 'review':
        return 'In Review';
      case 'blocked':
        return 'Blocked';
      case 'done':
        return 'Done';
      default:
        return 'Open';
    }
  }

  Color _priorityColor(String p) {
    switch (p) {
      case 'critical':
        return SovereignColors.accentDanger;
      case 'high':
        return const Color(0xFFFF6B35);
      case 'medium':
        return SovereignColors.accentWarning;
      default:
        return SovereignColors.textTertiary;
    }
  }

  Color _statusColor(String s) {
    switch (s) {
      case 'in_progress':
        return SovereignColors.soulLumina;
      case 'claimed':
        return SovereignColors.accentWarning;
      case 'review':
        return SovereignColors.soulJarvis;
      case 'blocked':
        return SovereignColors.accentDanger;
      case 'done':
        return SovereignColors.accentEncrypt;
      default:
        return SovereignColors.textTertiary;
    }
  }
}

class _DetailRow extends StatelessWidget {
  const _DetailRow({required this.label, required this.child});

  final String label;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SizedBox(
          width: 76,
          child: Text(
            label,
            style: const TextStyle(
              color: SovereignColors.textTertiary,
              fontSize: 12,
              fontWeight: FontWeight.w500,
            ),
          ),
        ),
        Expanded(child: child),
      ],
    );
  }
}
