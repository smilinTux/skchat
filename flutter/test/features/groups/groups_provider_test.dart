import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mocktail/mocktail.dart';
import 'package:skchat/data/conversation_repository.dart';
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

  Future<void> pumpAndDispose(ProviderContainer container) async {
    await Future<void>.delayed(Duration.zero);
    container.dispose();
  }

  group('GroupsNotifier', () {
    test('build returns empty list initially', () async {
      when(() => mockRepo.getAll()).thenAnswer((_) async => []);
      when(() => mockClient.isAlive()).thenAnswer((_) async => false);

      final container = createContainer();
      final groups = container.read(groupsProvider);

      expect(groups, isEmpty);
      await pumpAndDispose(container);
    });

    test('addGroup inserts a new group', () async {
      when(() => mockRepo.getAll()).thenAnswer((_) async => []);
      when(() => mockClient.isAlive()).thenAnswer((_) async => false);
      when(() => mockRepo.save(any())).thenAnswer((_) async {});

      final container = createContainer();
      final notifier = container.read(groupsProvider.notifier);

      await Future<void>.delayed(Duration.zero);

      final group = Conversation(
        peerId: 'penguin-kingdom',
        displayName: 'Penguin Kingdom',
        lastMessage: 'Welcome!',
        lastMessageTime: DateTime(2026, 2, 28),
        isGroup: true,
        memberCount: 4,
      );
      await notifier.addGroup(group);

      final state = container.read(groupsProvider);
      expect(state.length, 1);
      expect(state.first.peerId, 'penguin-kingdom');
      expect(state.first.isGroup, true);

      await pumpAndDispose(container);
    });

    test('addGroup does not duplicate existing group', () async {
      when(() => mockRepo.getAll()).thenAnswer((_) async => []);
      when(() => mockClient.isAlive()).thenAnswer((_) async => false);
      when(() => mockRepo.save(any())).thenAnswer((_) async {});

      final container = createContainer();
      final notifier = container.read(groupsProvider.notifier);

      await Future<void>.delayed(Duration.zero);

      final group = Conversation(
        peerId: 'test-group',
        displayName: 'Test Group',
        lastMessage: 'hey',
        lastMessageTime: DateTime(2026, 2, 28),
        isGroup: true,
      );
      await notifier.addGroup(group);
      await notifier.addGroup(group);

      expect(container.read(groupsProvider).length, 1);

      await pumpAndDispose(container);
    });

    test('updateGroup modifies existing group', () async {
      when(() => mockRepo.getAll()).thenAnswer((_) async => []);
      when(() => mockClient.isAlive()).thenAnswer((_) async => false);
      when(() => mockRepo.save(any())).thenAnswer((_) async {});

      final container = createContainer();
      final notifier = container.read(groupsProvider.notifier);

      await Future<void>.delayed(Duration.zero);

      final group = Conversation(
        peerId: 'builders',
        displayName: 'Builders',
        lastMessage: 'let us build',
        lastMessageTime: DateTime(2026, 2, 28),
        isGroup: true,
        memberCount: 3,
      );
      await notifier.addGroup(group);
      await notifier.updateGroup(group.copyWith(memberCount: 5));

      expect(container.read(groupsProvider).first.memberCount, 5);

      await pumpAndDispose(container);
    });

    test('removeGroup deletes by peerId', () async {
      when(() => mockRepo.getAll()).thenAnswer((_) async => []);
      when(() => mockClient.isAlive()).thenAnswer((_) async => false);
      when(() => mockRepo.save(any())).thenAnswer((_) async {});
      when(() => mockRepo.delete(any())).thenAnswer((_) async {});

      final container = createContainer();
      final notifier = container.read(groupsProvider.notifier);

      await Future<void>.delayed(Duration.zero);

      await notifier.addGroup(Conversation(
        peerId: 'to-remove',
        displayName: 'Temporary',
        lastMessage: 'bye',
        lastMessageTime: DateTime(2026, 2, 28),
        isGroup: true,
      ));

      expect(container.read(groupsProvider).length, 1);

      await notifier.removeGroup('to-remove');

      expect(container.read(groupsProvider), isEmpty);
      verify(() => mockRepo.delete('to-remove')).called(1);

      await pumpAndDispose(container);
    });
  });
}
