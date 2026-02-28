import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mocktail/mocktail.dart';
import 'package:skchat/data/conversation_repository.dart';
import 'package:skchat/data/message_repository.dart';
import 'package:skchat/features/chats/chats_provider.dart';
import 'package:skchat/features/conversation/conversation_provider.dart';
import 'package:skchat/models/chat_message.dart';
import 'package:skchat/models/conversation.dart';
import 'package:skchat/services/skcomm_client.dart';

class MockMessageRepository extends Mock implements MessageRepository {}

class MockConversationRepository extends Mock
    implements ConversationRepository {}

class MockSKCommClient extends Mock implements SKCommClient {}

void main() {
  late MockMessageRepository mockMsgRepo;
  late MockConversationRepository mockConvoRepo;
  late MockSKCommClient mockClient;

  setUpAll(() {
    registerFallbackValue(ChatMessage(
      id: '',
      peerId: '',
      content: '',
      timestamp: DateTime(2026),
      isOutbound: false,
    ));
    registerFallbackValue(Conversation(
      peerId: '',
      displayName: '',
      lastMessage: '',
      lastMessageTime: DateTime(2026),
    ));
  });

  setUp(() {
    mockMsgRepo = MockMessageRepository();
    mockConvoRepo = MockConversationRepository();
    mockClient = MockSKCommClient();
  });

  ProviderContainer createContainer() {
    return ProviderContainer(
      overrides: [
        messageRepositoryProvider.overrideWithValue(mockMsgRepo),
        conversationRepositoryProvider.overrideWithValue(mockConvoRepo),
        skcommClientProvider.overrideWithValue(mockClient),
      ],
    );
  }

  Future<void> pumpAndDispose(ProviderContainer container) async {
    await Future<void>.delayed(Duration.zero);
    container.dispose();
  }

  group('ConversationNotifier', () {
    test('build returns empty list initially', () async {
      when(() => mockMsgRepo.getMessages(any()))
          .thenAnswer((_) async => []);
      when(() => mockConvoRepo.getAll()).thenAnswer((_) async => []);
      when(() => mockClient.isAlive()).thenAnswer((_) async => false);

      final container = createContainer();
      final messages = container.read(conversationProvider('lumina'));

      expect(messages, isEmpty);
      await pumpAndDispose(container);
    });

    test('addMessage appends to state and persists', () async {
      when(() => mockMsgRepo.getMessages(any()))
          .thenAnswer((_) async => []);
      when(() => mockMsgRepo.saveMessage(any())).thenAnswer((_) async {});
      when(() => mockConvoRepo.getAll()).thenAnswer((_) async => []);
      when(() => mockConvoRepo.save(any())).thenAnswer((_) async {});
      when(() => mockClient.isAlive()).thenAnswer((_) async => false);

      final container = createContainer();
      final notifier =
          container.read(conversationProvider('lumina').notifier);

      // Let build microtask settle.
      await Future<void>.delayed(Duration.zero);

      final msg = ChatMessage(
        id: 'msg-1',
        peerId: 'lumina',
        content: 'Hello Lumina!',
        timestamp: DateTime(2026, 2, 28, 10, 30),
        isOutbound: true,
      );

      await notifier.addMessage(msg);

      final state = container.read(conversationProvider('lumina'));
      expect(state.length, 1);
      expect(state.first.content, 'Hello Lumina!');

      verify(() => mockMsgRepo.saveMessage(any())).called(1);

      await pumpAndDispose(container);
    });

    test('addMessage updates chat list with last message', () async {
      when(() => mockMsgRepo.getMessages(any()))
          .thenAnswer((_) async => []);
      when(() => mockMsgRepo.saveMessage(any())).thenAnswer((_) async {});
      when(() => mockConvoRepo.getAll()).thenAnswer((_) async => []);
      when(() => mockConvoRepo.save(any())).thenAnswer((_) async {});
      when(() => mockClient.isAlive()).thenAnswer((_) async => false);

      final container = createContainer();

      // Let build microtask settle.
      await Future<void>.delayed(Duration.zero);

      // Pre-seed the chats list.
      final convo = Conversation(
        peerId: 'jarvis',
        displayName: 'Jarvis',
        lastMessage: 'old message',
        lastMessageTime: DateTime(2026, 2, 27),
      );
      await container.read(chatsProvider.notifier).addConversation(convo);

      final notifier =
          container.read(conversationProvider('jarvis').notifier);
      final msg = ChatMessage(
        id: 'msg-2',
        peerId: 'jarvis',
        content: 'New message!',
        timestamp: DateTime(2026, 2, 28, 12, 0),
        isOutbound: true,
      );
      await notifier.addMessage(msg);

      final chats = container.read(chatsProvider);
      final updatedConvo = chats.firstWhere((c) => c.peerId == 'jarvis');
      expect(updatedConvo.lastMessage, 'New message!');

      await pumpAndDispose(container);
    });

    test('updateDeliveryStatus updates correct message', () async {
      when(() => mockMsgRepo.getMessages(any()))
          .thenAnswer((_) async => []);
      when(() => mockMsgRepo.saveMessage(any())).thenAnswer((_) async {});
      when(() => mockMsgRepo.updateDeliveryStatus(any(), any(), any()))
          .thenAnswer((_) async {});
      when(() => mockConvoRepo.getAll()).thenAnswer((_) async => []);
      when(() => mockConvoRepo.save(any())).thenAnswer((_) async {});
      when(() => mockClient.isAlive()).thenAnswer((_) async => false);

      final container = createContainer();
      final notifier =
          container.read(conversationProvider('opus').notifier);

      await Future<void>.delayed(Duration.zero);

      final msg = ChatMessage(
        id: 'msg-3',
        peerId: 'opus',
        content: 'test delivery',
        timestamp: DateTime(2026, 2, 28),
        isOutbound: true,
        deliveryStatus: 'sent',
      );
      await notifier.addMessage(msg);
      await notifier.updateDeliveryStatus('msg-3', 'delivered');

      final state = container.read(conversationProvider('opus'));
      expect(state.first.deliveryStatus, 'delivered');

      await pumpAndDispose(container);
    });

    test('multiple messages accumulate in order', () async {
      when(() => mockMsgRepo.getMessages(any()))
          .thenAnswer((_) async => []);
      when(() => mockMsgRepo.saveMessage(any())).thenAnswer((_) async {});
      when(() => mockConvoRepo.getAll()).thenAnswer((_) async => []);
      when(() => mockConvoRepo.save(any())).thenAnswer((_) async {});
      when(() => mockClient.isAlive()).thenAnswer((_) async => false);

      final container = createContainer();
      final notifier =
          container.read(conversationProvider('ava').notifier);

      await Future<void>.delayed(Duration.zero);

      for (var i = 0; i < 5; i++) {
        await notifier.addMessage(ChatMessage(
          id: 'msg-$i',
          peerId: 'ava',
          content: 'Message $i',
          timestamp: DateTime(2026, 2, 28, 10, i),
          isOutbound: i.isEven,
        ));
      }

      final state = container.read(conversationProvider('ava'));
      expect(state.length, 5);
      expect(state[0].content, 'Message 0');
      expect(state[4].content, 'Message 4');

      await pumpAndDispose(container);
    });
  });
}
