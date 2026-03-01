import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mocktail/mocktail.dart';

import 'package:skchat_mobile/core/transport/skcomm_client.dart';
import 'package:skchat_mobile/features/chat_list/chat_list_screen.dart';
import 'package:skchat_mobile/models/conversation.dart';

// ---------------------------------------------------------------------------
// Mock
// ---------------------------------------------------------------------------

class MockSKCommClient extends Mock implements SKCommClient {}

void main() {
  group('skcommClientProvider', () {
    test('creates an SKCommClient instance', () {
      final container = ProviderContainer();
      addTearDown(container.dispose);

      final client = container.read(skcommClientProvider);
      expect(client, isA<SKCommClient>());
    });
  });

  group('ChatListNotifier', () {
    late MockSKCommClient mockClient;
    late ProviderContainer container;

    setUp(() {
      mockClient = MockSKCommClient();
      container = ProviderContainer(
        overrides: [
          skcommClientProvider.overrideWithValue(mockClient),
        ],
      );
    });

    tearDown(() => container.dispose());

    test('returns empty list when daemon is unreachable', () async {
      when(() => mockClient.getConversations()).thenThrow(
        DioException(
          type: DioExceptionType.connectionTimeout,
          requestOptions: RequestOptions(path: '/'),
        ),
      );

      // Read the provider to trigger the build.
      final future = container.read(chatListProvider.future);
      final conversations = await future;

      expect(conversations, isEmpty);
    });

    test('returns conversations when daemon responds', () async {
      when(() => mockClient.getConversations()).thenAnswer(
        (_) async => [
          Conversation(
            id: 'lumina',
            participantId: 'lumina',
            participantName: 'Lumina',
            presenceStatus: PresenceStatus.online,
          ),
        ],
      );

      final conversations = await container.read(chatListProvider.future);

      // The notifier calls getConversations(); since we return a typed
      // List<Conversation>, the raw-JSON mapping branch is skipped.
      // The result should be the single conversation we provided.
      expect(conversations, hasLength(1));
      expect(conversations.first.participantId, 'lumina');
    });

    test('refresh re-fetches from daemon', () async {
      // First call returns empty.
      when(() => mockClient.getConversations())
          .thenAnswer((_) async => <Conversation>[]);

      await container.read(chatListProvider.future);

      // Now return data on the second call.
      when(() => mockClient.getConversations()).thenAnswer(
        (_) async => [
          Conversation(
            id: 'jarvis',
            participantId: 'jarvis',
            participantName: 'Jarvis',
          ),
        ],
      );

      await container.read(chatListProvider.notifier).refresh();
      final updated = container.read(chatListProvider).value;

      expect(updated, isNotNull);
      expect(updated!.any((c) => c.participantId == 'jarvis'), isTrue);
    });
  });
}
