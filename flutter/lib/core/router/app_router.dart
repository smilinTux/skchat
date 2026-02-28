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
import '../../features/groups/group_info_screen.dart';
import '../../features/groups/create_group_screen.dart';
import '../../features/chats/peer_picker_screen.dart';
import '../../features/profile/qr_login_screen.dart';
import '../../features/calls/outgoing_call_screen.dart';
import '../../features/calls/incoming_call_screen.dart';
import '../../features/calls/in_call_screen.dart';

/// Named route paths.
class AppRoutes {
  AppRoutes._();

  static const chats = '/chats';
  static const groups = '/groups';
  static const activity = '/activity';
  static const profile = '/profile';

  /// Peer picker: /chats/new
  static const peerPicker = '/chats/new';

  /// Conversation detail: /chats/:peerId
  static const conversation = '/chats/:peerId';

  /// Agent/peer identity card: /identity/:peerId
  static const identity = '/identity/:peerId';

  /// Group info & member management: /groups/:groupId/info
  static const groupInfo = '/groups/:groupId/info';

  /// Create new group: /groups/new
  static const createGroup = '/groups/new';

  /// CapAuth QR login screen: /login/qr
  static const qrLogin = '/login/qr';

  /// Outgoing call screen: /call/outgoing/:peerId
  static const outgoingCall = '/call/outgoing/:peerId';

  /// Incoming call screen: /call/incoming/:peerId
  static const incomingCall = '/call/incoming/:peerId';

  /// Active in-call screen: /call/active/:peerId
  static const inCall = '/call/active/:peerId';

  static String conversationPath(String peerId) => '/chats/$peerId';
  static String identityPath(String peerId) => '/identity/$peerId';
  static String groupInfoPath(String groupId) => '/groups/$groupId/info';
  static String outgoingCallPath(String peerId) => '/call/outgoing/$peerId';
  static String incomingCallPath(String peerId) => '/call/incoming/$peerId';
  static String inCallPath(String peerId) => '/call/active/$peerId';
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
                path: 'new',
                builder: (context, state) => const PeerPickerScreen(),
              ),
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
            routes: [
              GoRoute(
                path: 'new',
                builder: (context, state) => const CreateGroupScreen(),
              ),
              GoRoute(
                path: ':groupId/info',
                builder: (context, state) {
                  final groupId = state.pathParameters['groupId']!;
                  return GroupInfoScreen(groupId: groupId);
                },
              ),
            ],
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
      GoRoute(
        path: AppRoutes.qrLogin,
        builder: (context, state) => const QrLoginScreen(),
      ),

      // ── Call screens ────────────────────────────────────────────────────
      GoRoute(
        path: AppRoutes.outgoingCall,
        pageBuilder: (context, state) {
          final peerId = state.pathParameters['peerId']!;
          return MaterialPage(
            fullscreenDialog: true,
            child: OutgoingCallScreen(peerId: peerId),
          );
        },
      ),
      GoRoute(
        path: AppRoutes.incomingCall,
        pageBuilder: (context, state) {
          final peerId = state.pathParameters['peerId']!;
          return MaterialPage(
            fullscreenDialog: true,
            child: IncomingCallScreen(peerId: peerId),
          );
        },
      ),
      GoRoute(
        path: AppRoutes.inCall,
        pageBuilder: (context, state) {
          final peerId = state.pathParameters['peerId']!;
          return MaterialPage(
            fullscreenDialog: true,
            child: InCallScreen(peerId: peerId),
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
