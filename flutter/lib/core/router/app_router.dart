import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import '../../features/shell/app_shell.dart';
import '../../features/chats/chats_screen.dart';
import '../../features/groups/groups_screen.dart';
import '../../features/activity/activity_screen.dart';
import '../../features/profile/profile_screen.dart';
import '../../features/conversation/conversation_screen.dart';
import '../../features/identity/identity_card_screen.dart';

/// Named route paths.
class AppRoutes {
  AppRoutes._();

  static const chats = '/chats';
  static const groups = '/groups';
  static const activity = '/activity';
  static const profile = '/profile';

  /// Conversation detail: /chats/:peerId
  static const conversation = '/chats/:peerId';

  /// Agent/peer identity card: /identity/:peerId
  static const identity = '/identity/:peerId';

  static String conversationPath(String peerId) => '/chats/$peerId';
  static String identityPath(String peerId) => '/identity/$peerId';
}

/// GoRouter provider — uses shell routes for the bottom nav structure.
final appRouterProvider = Provider<GoRouter>((ref) {
  return GoRouter(
    initialLocation: AppRoutes.chats,
    debugLogDiagnostics: false,
    routes: [
      ShellRoute(
        builder: (context, state, child) => AppShell(child: child),
        routes: [
          GoRoute(
            path: AppRoutes.chats,
            pageBuilder: (context, state) => _noTransitionPage(
              state,
              const ChatsScreen(),
            ),
            routes: [
              GoRoute(
                path: ':peerId',
                builder: (context, state) {
                  final peerId = state.pathParameters['peerId']!;
                  return ConversationScreen(peerId: peerId);
                },
              ),
            ],
          ),
          GoRoute(
            path: AppRoutes.groups,
            pageBuilder: (context, state) => _noTransitionPage(
              state,
              const GroupsScreen(),
            ),
          ),
          GoRoute(
            path: AppRoutes.activity,
            pageBuilder: (context, state) => _noTransitionPage(
              state,
              const ActivityScreen(),
            ),
          ),
          GoRoute(
            path: AppRoutes.profile,
            pageBuilder: (context, state) => _noTransitionPage(
              state,
              const ProfileScreen(),
            ),
          ),
        ],
      ),
      GoRoute(
        path: AppRoutes.identity,
        builder: (context, state) {
          final args = state.extra as IdentityCardArgs;
          return IdentityCardScreen(
            conversation: args.conversation,
            onSendMessage: args.onSendMessage,
          );
        },
      ),
    ],
  );
});

/// Instant tab switch — no transition animation on tab change.
/// Push navigation (conversations) uses the default spring transition.
NoTransitionPage<void> _noTransitionPage(GoRouterState state, Widget child) {
  return NoTransitionPage<void>(key: state.pageKey, child: child);
}
