import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import '../../core/router/app_router.dart';
import '../../core/theme/theme.dart';
import '../../models/chat_message.dart';
import '../chats/chats_provider.dart';
import '../identity/identity_card_screen.dart';
import '../../services/skcomm_sync.dart';
import 'conversation_provider.dart';
import 'widgets/message_bubble.dart';
import 'widgets/typing_indicator.dart';
import 'widgets/input_bar.dart';

/// Conversation screen — shows message bubbles for a 1:1 or group chat.
/// AppBar shows soul-color avatar, name, presence, and encryption indicator.
/// Messages: outbound right (user's soul-color tint), inbound left (glass + accent edge).
/// Typing indicator: personality-aware per PRD.
/// Input bar: glass-surface, bottom-pinned.
class ConversationScreen extends ConsumerWidget {
  const ConversationScreen({super.key, required this.peerId});

  final String peerId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final conversations = ref.watch(chatsProvider);
    final conversation = conversations.firstWhere(
      (c) => c.peerId == peerId,
      orElse: () => conversations.first,
    );
    final messages = ref.watch(conversationProvider(peerId));
    final soul = conversation.resolvedSoulColor;

    return Scaffold(
      backgroundColor: SovereignColors.surfaceBase,
      appBar: _buildAppBar(context, conversation, soul),
      body: Column(
        children: [
          // Message list
          Expanded(
            child: _MessageList(
              messages: messages,
              soulColor: soul,
              isTyping: conversation.isTyping,
              peerName: conversation.displayName,
              isAgent: conversation.isAgent,
            ),
          ),

          // Typing indicator strip
          if (conversation.isTyping)
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
              child: Align(
                alignment: Alignment.centerLeft,
                child: TypingIndicator(
                  peerName: conversation.displayName,
                  isAgent: conversation.isAgent,
                  soulColor: soul,
                ),
              ),
            ),

          // Input bar — adds message optimistically, then sends to daemon.
          InputBar(
            soulColor: soul,
            onSend: (text) async {
              final tempId = '${DateTime.now().millisecondsSinceEpoch}';
              // Optimistic insert so the user sees the message immediately.
              ref.read(conversationProvider(peerId).notifier).addMessage(
                ChatMessage(
                  id: tempId,
                  peerId: peerId,
                  content: text,
                  timestamp: DateTime.now(),
                  isOutbound: true,
                  deliveryStatus: 'sent',
                ),
              );
              // Fire-and-forget to daemon; delivery status updated on next poll.
              ref.read(skcommSyncProvider.notifier).sendMessage(
                peerId: peerId,
                content: text,
              );
            },
          ),
        ],
      ),
    );
  }

  PreferredSizeWidget _buildAppBar(
    BuildContext context,
    conversation,
    Color soul,
  ) {
    final tt = Theme.of(context).textTheme;

    return AppBar(
      backgroundColor: SovereignColors.surfaceBase,
      leading: IconButton(
        icon: const Icon(Icons.arrow_back_rounded),
        onPressed: () => Navigator.of(context).maybePop(),
      ),
      titleSpacing: 0,
      title: GestureDetector(
        onTap: () => context.push(
          AppRoutes.identityPath(conversation.peerId),
          extra: IdentityCardArgs(conversation: conversation),
        ),
        child: Row(
          children: [
            SoulAvatar(
              soulColor: soul,
              initials: conversation.resolvedInitials,
              isOnline: conversation.isOnline,
              isAgent: conversation.isAgent,
              size: 36,
            ),
            const SizedBox(width: 10),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    conversation.displayName,
                    style: tt.titleMedium,
                    overflow: TextOverflow.ellipsis,
                  ),
                  Text(
                    _presenceText(conversation),
                    style: tt.labelSmall?.copyWith(
                      color: conversation.isOnline
                          ? soul
                          : SovereignColors.textTertiary,
                    ),
                    overflow: TextOverflow.ellipsis,
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
      actions: [
        const EncryptBadge(size: 16),
        const SizedBox(width: 4),
        IconButton(
          icon: const Icon(Icons.call_outlined),
          onPressed: () {},
          tooltip: 'Voice call',
        ),
        IconButton(
          icon: const Icon(Icons.more_vert_rounded),
          onPressed: () {},
          tooltip: 'More options',
        ),
      ],
    );
  }

  String _presenceText(dynamic conv) {
    if (conv.isTyping) return 'composing...';
    if (conv.isOnline) return 'online';
    if (conv.isAgent) return 'agent · offline';
    return 'last seen recently';
  }
}

/// Scrollable message list with auto-scroll-to-bottom behavior.
class _MessageList extends StatefulWidget {
  const _MessageList({
    required this.messages,
    required this.soulColor,
    required this.isTyping,
    required this.peerName,
    required this.isAgent,
  });

  final List<ChatMessage> messages;
  final Color soulColor;
  final bool isTyping;
  final String peerName;
  final bool isAgent;

  @override
  State<_MessageList> createState() => _MessageListState();
}

class _MessageListState extends State<_MessageList> {
  final _scrollController = ScrollController();

  @override
  void didUpdateWidget(_MessageList oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.messages.length != oldWidget.messages.length) {
      _scrollToBottom();
    }
  }

  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scrollController.hasClients) {
        _scrollController.animateTo(
          _scrollController.position.maxScrollExtent,
          duration: const Duration(milliseconds: 300),
          curve: Curves.easeOutCubic,
        );
      }
    });
  }

  @override
  void dispose() {
    _scrollController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return ListView.builder(
      controller: _scrollController,
      padding: const EdgeInsets.symmetric(vertical: 8),
      itemCount: widget.messages.length,
      itemBuilder: (context, index) {
        final message = widget.messages[index];
        return MessageBubble(
          message: message,
          soulColor: widget.soulColor,
        );
      },
    );
  }
}
