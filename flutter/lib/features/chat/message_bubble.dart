import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import '../../models/chat_message.dart';

/// Simple chat bubble widget.
///
/// - User messages (isOutbound=true): right-aligned, blue background.
/// - Agent/peer messages (isOutbound=false): left-aligned, grey background.
/// - Timestamp displayed below each bubble.
class ChatMessageBubble extends StatelessWidget {
  const ChatMessageBubble({super.key, required this.message});

  final ChatMessage message;

  static const _blue = Color(0xFF1565C0);
  static const _grey = Color(0xFF2A2A2E);
  static const _textColor = Colors.white;
  static const _timestampColor = Color(0xFF909090);

  @override
  Widget build(BuildContext context) {
    final isOut = message.isOutbound;

    return Padding(
      padding: EdgeInsets.only(
        left: isOut ? 64 : 12,
        right: isOut ? 12 : 64,
        top: 3,
        bottom: 3,
      ),
      child: Align(
        alignment: isOut ? Alignment.centerRight : Alignment.centerLeft,
        child: Column(
          crossAxisAlignment:
              isOut ? CrossAxisAlignment.end : CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            // Bubble
            Container(
              padding:
                  const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
              decoration: BoxDecoration(
                color: isOut ? _blue : _grey,
                borderRadius: BorderRadius.only(
                  topLeft: const Radius.circular(16),
                  topRight: const Radius.circular(16),
                  bottomLeft: Radius.circular(isOut ? 16 : 4),
                  bottomRight: Radius.circular(isOut ? 4 : 16),
                ),
              ),
              child: Text(
                message.content,
                style: const TextStyle(
                  color: _textColor,
                  fontSize: 14,
                  height: 1.4,
                ),
              ),
            ),
            const SizedBox(height: 2),
            // Timestamp
            Text(
              DateFormat('h:mm a').format(message.timestamp),
              style: const TextStyle(
                fontSize: 11,
                color: _timestampColor,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
