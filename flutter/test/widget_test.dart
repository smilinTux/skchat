import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mocktail/mocktail.dart';
import 'package:skchat/data/conversation_repository.dart';
import 'package:skchat/data/message_repository.dart';
import 'package:skchat/main.dart';
import 'package:skchat/models/conversation.dart';
import 'package:skchat/services/skcomm_client.dart';
import 'package:skchat/services/skcomm_sync.dart';

class MockSKCommClient extends Mock implements SKCommClient {}

class MockConversationRepository extends Mock
    implements ConversationRepository {}

class MockMessageRepository extends Mock implements MessageRepository {}

void main() {
  testWidgets('SKChatApp smoke test', (WidgetTester tester) async {
    final mockClient = MockSKCommClient();
    final mockConvoRepo = MockConversationRepository();
    final mockMsgRepo = MockMessageRepository();

    when(() => mockClient.isAlive()).thenAnswer((_) async => false);
    when(() => mockClient.getInbox()).thenAnswer((_) async => []);
    when(() => mockConvoRepo.getAll()).thenAnswer((_) async => <Conversation>[]);

    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          skcommClientProvider.overrideWithValue(mockClient),
          skcommSyncProvider.overrideWith(() => _NoOpSyncNotifier()),
          conversationRepositoryProvider.overrideWithValue(mockConvoRepo),
          messageRepositoryProvider.overrideWithValue(mockMsgRepo),
        ],
        child: const SKChatApp(),
      ),
    );
    await tester.pump();
    // App renders without crashing.
    expect(find.byType(MaterialApp), findsOneWidget);
  });
}

/// A no-op sync notifier that skips timer creation for tests.
class _NoOpSyncNotifier extends SKCommSyncNotifier {
  @override
  DaemonState build() => const DaemonState(status: DaemonStatus.offline);
}
