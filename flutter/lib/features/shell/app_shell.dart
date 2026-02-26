import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import '../../core/theme/theme.dart';
import '../../core/router/app_router.dart';
import '../../services/skcomm_sync.dart';

/// AppShell wraps all main tab screens with the Sovereign Glass bottom nav bar.
/// Shows a subtle offline banner when the SKComm daemon is unreachable.
class AppShell extends ConsumerWidget {
  const AppShell({super.key, required this.child});

  final Widget child;

  static const _tabs = [
    _TabItem(
      label: 'Chats',
      icon: Icons.chat_bubble_outline_rounded,
      activeIcon: Icons.chat_bubble_rounded,
      path: AppRoutes.chats,
    ),
    _TabItem(
      label: 'Groups',
      icon: Icons.group_outlined,
      activeIcon: Icons.group_rounded,
      path: AppRoutes.groups,
    ),
    _TabItem(
      label: 'Activity',
      icon: Icons.notifications_outlined,
      activeIcon: Icons.notifications_rounded,
      path: AppRoutes.activity,
    ),
    _TabItem(
      label: 'Me',
      icon: Icons.person_outline_rounded,
      activeIcon: Icons.person_rounded,
      path: AppRoutes.profile,
    ),
  ];

  int _indexFor(BuildContext context, WidgetRef _) {
    final location = GoRouterState.of(context).matchedLocation;
    if (location.startsWith(AppRoutes.groups)) return 1;
    if (location.startsWith(AppRoutes.activity)) return 2;
    if (location.startsWith(AppRoutes.profile)) return 3;
    return 0; // chats
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final currentIndex = _indexFor(context, ref);
    final daemonState = ref.watch(skcommSyncProvider);
    final isOffline = daemonState.status == DaemonStatus.offline;

    return Scaffold(
      backgroundColor: SovereignColors.surfaceBase,
      extendBody: true,
      body: Column(
        children: [
          // Offline banner — shown when daemon is unreachable.
          if (isOffline)
            Material(
              color: SovereignColors.accentWarning.withValues(alpha: 0.15),
              child: const SafeArea(
                bottom: false,
                child: Padding(
                  padding: EdgeInsets.symmetric(horizontal: 16, vertical: 6),
                  child: Row(
                    children: [
                      Icon(
                        Icons.cloud_off_outlined,
                        size: 14,
                        color: SovereignColors.accentWarning,
                      ),
                      SizedBox(width: 6),
                      Text(
                        'SKComm daemon offline — messages will queue',
                        style: TextStyle(
                          fontSize: 12,
                          color: SovereignColors.accentWarning,
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),
          Expanded(child: child),
        ],
      ),
      bottomNavigationBar: GlassNavBar(
        child: Row(
          mainAxisAlignment: MainAxisAlignment.spaceAround,
          children: List.generate(_tabs.length, (i) {
            final tab = _tabs[i];
            final isActive = i == currentIndex;
            final accentColor = Theme.of(context).colorScheme.primary;

            return Expanded(
              child: InkWell(
                onTap: () => context.go(tab.path),
                borderRadius: BorderRadius.circular(12),
                child: AnimatedContainer(
                  duration: const Duration(milliseconds: 200),
                  curve: Curves.easeOutCubic,
                  padding: const EdgeInsets.symmetric(vertical: 10),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      AnimatedSwitcher(
                        duration: const Duration(milliseconds: 200),
                        child: Icon(
                          isActive ? tab.activeIcon : tab.icon,
                          key: ValueKey(isActive),
                          size: 24,
                          color: isActive
                              ? accentColor
                              : SovereignColors.textSecondary,
                        ),
                      ),
                      const SizedBox(height: 4),
                      Text(
                        tab.label,
                        style: Theme.of(context).textTheme.labelSmall?.copyWith(
                          color: isActive
                              ? accentColor
                              : SovereignColors.textSecondary,
                          fontWeight: isActive
                              ? FontWeight.w600
                              : FontWeight.w400,
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            );
          }),
        ),
      ),
    );
  }
}

class _TabItem {
  const _TabItem({
    required this.label,
    required this.icon,
    required this.activeIcon,
    required this.path,
  });

  final String label;
  final IconData icon;
  final IconData activeIcon;
  final String path;
}
