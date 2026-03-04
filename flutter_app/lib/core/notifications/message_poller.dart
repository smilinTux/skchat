import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../transport/skcomm_client.dart';
import 'notification_service.dart';

/// Riverpod [StreamProvider] that periodically polls the SKComm daemon inbox
/// and fires a local notification for every new message received.
///
/// Yields the cumulative count of new messages delivered this session.
/// Ignores messages already seen (tracked by [ChatMessage.id] in memory).
///
/// Poll interval: 10 seconds. The provider lives as long as it is watched.
final messagePollerProvider = StreamProvider<int>((ref) {
  return _messagePollerStream(ref);
});

Stream<int> _messagePollerStream(Ref ref) async* {
  final client = ref.read(skcommClientProvider);
  final notif = NotificationService();
  final seen = <String>{};
  var total = 0;

  while (true) {
    await Future<void>.delayed(const Duration(seconds: 10));
    try {
      final messages = await client.pollInbox();
      var newThisTick = 0;
      for (final msg in messages) {
        if (seen.contains(msg.id)) continue;
        seen.add(msg.id);
        newThisTick++;
        total++;
        await notif.showMessageNotification(
          senderName: msg.senderName,
          messagePreview: msg.content,
        );
      }
      if (newThisTick > 0) yield total;
    } catch (_) {
      // Daemon offline — wait for next tick.
    }
  }
}
