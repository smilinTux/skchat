import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mocktail/mocktail.dart';
import 'package:skchat/data/conversation_repository.dart';
import 'package:skchat/features/groups/create_group_screen.dart';
import 'package:skchat/features/groups/groups_provider.dart';
import 'package:skchat/models/conversation.dart';
import 'package:skchat/services/skcomm_client.dart';

class MockSKCommClient extends Mock implements SKCommClient {}

class MockConversationRepository extends Mock
    implements ConversationRepository {}

final _dummyConversation = Conversation(
  peerId: '',
  displayName: '',
  lastMessage: '',
  lastMessageTime: DateTime(2026),
);

Widget _wrap({
  required MockSKCommClient client,
  required MockConversationRepository repo,
}) {
  return ProviderScope(
    overrides: [
      skcommClientProvider.overrideWithValue(client),
      conversationRepositoryProvider.overrideWithValue(repo),
    ],
    child: const MaterialApp(
      home: CreateGroupScreen(),
    ),
  );
}

void main() {
  late MockSKCommClient mockClient;
  late MockConversationRepository mockRepo;

  setUpAll(() {
    registerFallbackValue(_dummyConversation);
  });

  setUp(() {
    mockClient = MockSKCommClient();
    mockRepo = MockConversationRepository();
  });

  group('CreateGroupScreen', () {
    testWidgets('renders name and description fields', (tester) async {
      when(() => mockClient.isAlive()).thenAnswer((_) async => false);
      when(() => mockClient.getPeers()).thenAnswer((_) async => []);
      when(() => mockRepo.getAll()).thenAnswer((_) async => []);

      await tester.pumpWidget(_wrap(client: mockClient, repo: mockRepo));
      await tester.pump();

      expect(find.text('New Group'), findsOneWidget);
      expect(find.widgetWithText(TextField, 'Group name'), findsWidgets);
    });

    testWidgets('Create button disabled when name is empty', (tester) async {
      when(() => mockClient.isAlive()).thenAnswer((_) async => false);
      when(() => mockClient.getPeers()).thenAnswer((_) async => []);
      when(() => mockRepo.getAll()).thenAnswer((_) async => []);

      await tester.pumpWidget(_wrap(client: mockClient, repo: mockRepo));
      await tester.pump();

      // The Create button should be present but inactive when name is empty.
      final btn = find.text('Create');
      expect(btn, findsOneWidget);

      final textBtn = tester.widget<TextButton>(
        find.ancestor(of: btn, matching: find.byType(TextButton)),
      );
      expect(textBtn.onPressed, isNull);
    });

    testWidgets('Create button enabled after typing name', (tester) async {
      when(() => mockClient.isAlive()).thenAnswer((_) async => false);
      when(() => mockClient.getPeers()).thenAnswer((_) async => []);
      when(() => mockRepo.getAll()).thenAnswer((_) async => []);

      await tester.pumpWidget(_wrap(client: mockClient, repo: mockRepo));
      await tester.pump();

      // Type a name.
      await tester.enterText(
          find.widgetWithText(TextField, 'Group name'), 'Builders');
      await tester.pump();

      final btn = find.text('Create');
      final textBtn = tester.widget<TextButton>(
        find.ancestor(of: btn, matching: find.byType(TextButton)),
      );
      expect(textBtn.onPressed, isNotNull);
    });

    testWidgets('shows encryption info banner', (tester) async {
      when(() => mockClient.isAlive()).thenAnswer((_) async => false);
      when(() => mockClient.getPeers()).thenAnswer((_) async => []);
      when(() => mockRepo.getAll()).thenAnswer((_) async => []);

      await tester.pumpWidget(_wrap(client: mockClient, repo: mockRepo));
      await tester.pump();

      expect(find.textContaining('AES-256-GCM'), findsOneWidget);
    });

    testWidgets('shows daemon-offline message when peers fail to load',
        (tester) async {
      when(() => mockClient.isAlive())
          .thenAnswer((_) async => throw Exception('offline'));
      when(() => mockClient.getPeers())
          .thenAnswer((_) async => throw Exception('offline'));
      when(() => mockRepo.getAll()).thenAnswer((_) async => []);

      await tester.pumpWidget(_wrap(client: mockClient, repo: mockRepo));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      // Error state shows a helpful message.
      expect(find.textContaining('offline'), findsWidgets);
    });
  });
}
