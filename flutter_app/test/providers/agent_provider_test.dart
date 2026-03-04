import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mocktail/mocktail.dart';

import 'package:skchat_mobile/core/providers/agent_provider.dart';
import 'package:skchat_mobile/core/transport/skcomm_client.dart';

class MockSKCommClient extends Mock implements SKCommClient {}

void main() {
  group('AgentInfo.fromJson', () {
    test('maps name field', () {
      final info = AgentInfo.fromJson({'name': 'lumina', 'fingerprint': 'abc'});
      expect(info.name, 'lumina');
      expect(info.fingerprint, 'abc');
    });

    test('falls back to agent field when name is absent', () {
      final info = AgentInfo.fromJson({'agent': 'jarvis'});
      expect(info.name, 'jarvis');
    });

    test('returns empty string for unknown name', () {
      final info = AgentInfo.fromJson({});
      expect(info.name, '');
    });
  });

  group('AgentNotifier', () {
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
      when(() => mockClient.getAgents())
          .thenThrow(SKCommException('connection refused'));

      final agents = await container.read(agentProvider.future);
      expect(agents, isEmpty);
    });

    test('maps raw JSON to AgentInfo list', () async {
      when(() => mockClient.getAgents()).thenAnswer((_) async => [
            {'name': 'lumina', 'fingerprint': 'fp1', 'state': 'active'},
            {'name': 'jarvis', 'fingerprint': 'fp2', 'state': 'idle'},
          ]);

      final agents = await container.read(agentProvider.future);

      expect(agents, hasLength(2));
      expect(agents[0].name, 'lumina');
      expect(agents[0].fingerprint, 'fp1');
      expect(agents[0].state, 'active');
      expect(agents[1].name, 'jarvis');
    });

    test('refresh re-fetches from daemon', () async {
      when(() => mockClient.getAgents())
          .thenAnswer((_) async => <Map<String, dynamic>>[]);

      await container.read(agentProvider.future);

      when(() => mockClient.getAgents()).thenAnswer((_) async => [
            {'name': 'opus'},
          ]);

      await container.read(agentProvider.notifier).refresh();
      final updated = container.read(agentProvider).value;

      expect(updated, isNotNull);
      expect(updated!.any((a) => a.name == 'opus'), isTrue);
    });
  });
}
