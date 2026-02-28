import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mocktail/mocktail.dart';
import 'package:skchat/data/conversation_repository.dart';
import 'package:skchat/features/chats/chats_provider.dart';
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

  ProviderContainer createContainer() {
    return ProviderContainer(
      overrides: [
        skcommClientProvider.overrideWithValue(mockClient),
        conversationRepositoryProvider.overrideWithValue(mockRepo),
      ],
    );
  }

  /// Let all pending microtasks complete before disposing.
  Future<void> pumpAndDispose(ProviderContainer container) async {
    await Future<void>.delayed(Duration.zero);
    container.dispose();
  }

  group('ChatsNotifier', () {
    test('build returns empty list initially', () async {
      when(() => mockRepo.getAll()).thenAnswer((_) async => []);
      when(() => mockClient.isAlive()).thenAnswer((_) async => false);

      final container = createContainer();
      final chats = container.read(chatsProvider);

      expect(chats, isEmpty);
      await pumpAndDispose(container);
    });

    test('updateConversation replaces matching conversation', () async {
      when(() => mockRepo.getAll()).thenAnswer((_) async => []);
      when(() => mockClient.isAlive()).thenAnswer((_) async => false);
      when(() => mockRepo.save(any())).thenAnswer((_) async {});

      final container = createContainer();
      final notifier = container.read(chatsProvider.notifier);

      // Let the build microtask settle.
      await Future<void>.delayed(Duration.zero);

      final original = Conversation(
        peerId: 'lumina',
        displayName: 'Lumina',
        lastMessage: 'Hello',
        lastMessageTime: DateTime(2026, 2, 28),
      );
      await notifier.addConversation(original);

      final updated = original.copyWith(lastMessage: 'Updated message');
      await notifier.updateConversation(updated);

      final state = container.read(chatsProvider);
      expect(state.length, 1);
      expect(state.first.lastMessage, 'Updated message');

      await pumpAndDispose(container);
    });

    test('addConversation does not duplicate existing peerId', () async {
      when(() => mockRepo.getAll()).thenAnswer((_) async => []);
      when(() => mockClient.isAlive()).thenAnswer((_) async => false);
      when(() => mockRepo.save(any())).thenAnswer((_) async {});

      final container = createContainer();
      final notifier = container.read(chatsProvider.notifier);

      await Future<void>.delayed(Duration.zero);

      final convo = Conversation(
        peerId: 'jarvis',
        displayName: 'Jarvis',
        lastMessage: 'msg1',
        lastMessageTime: DateTime(2026, 2, 28),
      );
      await notifier.addConversation(convo);
      await notifier.addConversation(convo);

      final state = container.read(chatsProvider);
      expect(state.length, 1);

      await pumpAndDispose(container);
    });

    test('setTyping updates correct conversation', () async {
      when(() => mockRepo.getAll()).thenAnswer((_) async => []);
      when(() => mockClient.isAlive()).thenAnswer((_) async => false);
      when(() => mockRepo.save(any())).thenAnswer((_) async {});

      final container = createContainer();
      final notifier = container.read(chatsProvider.notifier);

      await Future<void>.delayed(Duration.zero);

      await notifier.addConversation(Conversation(
        peerId: 'lumina',
        displayName: 'Lumina',
        lastMessage: 'hi',
        lastMessageTime: DateTime(2026, 2, 28),
      ));

      notifier.setTyping('lumina', typing: true);
      expect(container.read(chatsProvider).first.isTyping, true);

      notifier.setTyping('lumina', typing: false);
      expect(container.read(chatsProvider).first.isTyping, false);

      await pumpAndDispose(container);
    });

    test('markRead resets unread count to zero', () async {
      when(() => mockRepo.getAll()).thenAnswer((_) async => []);
      when(() => mockClient.isAlive()).thenAnswer((_) async => false);
      when(() => mockRepo.save(any())).thenAnswer((_) async {});

      final container = createContainer();
      final notifier = container.read(chatsProvider.notifier);

      await Future<void>.delayed(Duration.zero);

      await notifier.addConversation(Conversation(
        peerId: 'opus',
        displayName: 'Opus',
        lastMessage: 'test',
        lastMessageTime: DateTime(2026, 2, 28),
        unreadCount: 5,
      ));

      await notifier.markRead('opus');

      expect(container.read(chatsProvider).first.unreadCount, 0);

      await pumpAndDispose(container);
    });

    test('markRead for non-existent peer does nothing', () async {
      when(() => mockRepo.getAll()).thenAnswer((_) async => []);
      when(() => mockClient.isAlive()).thenAnswer((_) async => false);

      final container = createContainer();
      container.read(chatsProvider.notifier);

      await Future<void>.delayed(Duration.zero);

      await container.read(chatsProvider.notifier).markRead('non-existent');

      expect(container.read(chatsProvider), isEmpty);

      await pumpAndDispose(container);
    });
  });
}
