import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mocktail/mocktail.dart';

import 'package:skchat_mobile/core/providers/memory_provider.dart';
import 'package:skchat_mobile/core/transport/skcomm_client.dart';

class MockSKCommClient extends Mock implements SKCommClient {}

void main() {
  group('MemoryEntry.fromJson', () {
    test('maps all fields', () {
      final entry = MemoryEntry.fromJson({
        'id': 'mem1',
        'content': 'penguin intel',
        'tags': ['pengu', 'test'],
        'scope': 'mid-term',
        'created_at': '2026-03-02T00:00:00Z',
      });
      expect(entry.id, 'mem1');
      expect(entry.content, 'penguin intel');
      expect(entry.tags, containsAll(['pengu', 'test']));
      expect(entry.scope, 'mid-term');
      expect(entry.createdAt, isNotNull);
    });

    test('handles missing optional fields', () {
      final entry = MemoryEntry.fromJson({'id': 'x', 'content': 'hello'});
      expect(entry.tags, isEmpty);
      expect(entry.scope, isNull);
      expect(entry.createdAt, isNull);
    });
  });

  group('MemoryQuery equality', () {
    test('same query is equal', () {
      expect(const MemoryQuery(query: 'penguin'),
          equals(const MemoryQuery(query: 'penguin')));
    });

    test('different queries are not equal', () {
      expect(const MemoryQuery(query: 'a'),
          isNot(equals(const MemoryQuery(query: 'b'))));
    });
  });

  group('memoryProvider (FutureProvider.family)', () {
    late MockSKCommClient mockClient;
    late ProviderContainer container;

    setUp(() {
      mockClient = MockSKCommClient();
      container = ProviderContainer(
        overrides: [skcommClientProvider.overrideWithValue(mockClient)],
      );
    });

    tearDown(() => container.dispose());

    test('returns empty list when daemon is unreachable', () async {
      when(() => mockClient.getMemoryEntries(query: any(named: 'query')))
          .thenThrow(SKCommException('offline'));

      final entries = await container.read(
        memoryProvider(const MemoryQuery()).future,
      );
      expect(entries, isEmpty);
    });

    test('returns filtered entries for search query', () async {
      when(() => mockClient.getMemoryEntries(query: 'penguin'))
          .thenAnswer((_) async => [
                {'id': '1', 'content': 'penguin intel', 'tags': ['pengu']},
              ]);

      final entries = await container.read(
        memoryProvider(const MemoryQuery(query: 'penguin')).future,
      );
      expect(entries, hasLength(1));
      expect(entries.first.content, 'penguin intel');
    });

    test('calls getMemoryEntries with null query for broad fetch', () async {
      when(() => mockClient.getMemoryEntries(query: null))
          .thenAnswer((_) async => [
                {'id': '1', 'content': 'all memories'},
              ]);

      final entries = await container.read(
        memoryProvider(const MemoryQuery()).future,
      );
      verify(() => mockClient.getMemoryEntries(query: null)).called(1);
      expect(entries, hasLength(1));
    });
  });

  group('MemoryNotifier', () {
    late MockSKCommClient mockClient;
    late ProviderContainer container;

    setUp(() {
      mockClient = MockSKCommClient();
      container = ProviderContainer(
        overrides: [skcommClientProvider.overrideWithValue(mockClient)],
      );
    });

    tearDown(() => container.dispose());

    test('build fetches all entries', () async {
      when(() => mockClient.getMemoryEntries(query: any(named: 'query')))
          .thenAnswer((_) async => [
                {'id': '1', 'content': 'first memory'},
                {'id': '2', 'content': 'second memory'},
              ]);

      final entries =
          await container.read(memoryNotifierProvider.future);
      expect(entries, hasLength(2));
    });

    test('store calls storeMemory and refreshes list', () async {
      when(() => mockClient.getMemoryEntries(query: any(named: 'query')))
          .thenAnswer((_) async => <Map<String, dynamic>>[]);
      when(() => mockClient.storeMemory(
            content: any(named: 'content'),
            tags: any(named: 'tags'),
            scope: any(named: 'scope'),
          )).thenAnswer((_) async {});

      await container.read(memoryNotifierProvider.future);

      when(() => mockClient.getMemoryEntries(query: any(named: 'query')))
          .thenAnswer((_) async => [
                {'id': 'new', 'content': 'recalled later'},
              ]);

      await container.read(memoryNotifierProvider.notifier).store(
            content: 'recalled later',
            tags: ['short-term'],
          );

      final updated = container.read(memoryNotifierProvider).value;
      expect(updated, isNotNull);
      expect(updated!.any((e) => e.content == 'recalled later'), isTrue);
    });

    test('store is silent when daemon is offline', () async {
      when(() => mockClient.getMemoryEntries(query: any(named: 'query')))
          .thenAnswer((_) async => <Map<String, dynamic>>[]);
      when(() => mockClient.storeMemory(
            content: any(named: 'content'),
            tags: any(named: 'tags'),
            scope: any(named: 'scope'),
          )).thenThrow(SKCommException('offline'));

      await container.read(memoryNotifierProvider.future);

      // Should not throw.
      await container
          .read(memoryNotifierProvider.notifier)
          .store(content: 'test');
    });

    test('refresh re-fetches from daemon', () async {
      when(() => mockClient.getMemoryEntries(query: any(named: 'query')))
          .thenAnswer((_) async => <Map<String, dynamic>>[]);

      await container.read(memoryNotifierProvider.future);

      when(() => mockClient.getMemoryEntries(query: any(named: 'query')))
          .thenAnswer((_) async => [
                {'id': '99', 'content': 'fresh memory'},
              ]);

      await container.read(memoryNotifierProvider.notifier).refresh();
      final updated = container.read(memoryNotifierProvider).value;
      expect(updated!.any((e) => e.id == '99'), isTrue);
    });
  });
}
