import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import 'core/theme/sovereign_glass.dart';
import 'features/chat_list/chat_list_screen.dart';
import 'features/conversation/conversation_screen.dart';

class SKChatApp extends ConsumerWidget {
  const SKChatApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final router = GoRouter(
      initialLocation: '/chats',
      routes: [
        GoRoute(
          path: '/chats',
          builder: (context, state) => const ChatListScreen(),
        ),
        GoRoute(
          path: '/conversation/:id',
          builder: (context, state) {
            final conversationId = state.pathParameters['id']!;
            return ConversationScreen(conversationId: conversationId);
          },
        ),
      ],
    );

    return MaterialApp.router(
      title: 'SKChat',
      debugShowCheckedModeBanner: false,
      theme: SovereignGlassTheme.darkTheme(),
      routerConfig: router,
    );
  }
}
