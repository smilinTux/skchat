import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mocktail/mocktail.dart';

import 'package:skchat_mobile/core/providers/presence_provider.dart';
import 'package:skchat_mobile/core/transport/skcomm_client.dart';

class MockSKCommClient extends Mock implements SKCommClient {}

void main() {
  group('PeerPresence.fromJson', () {
    test('maps all fields', () {
      final presence = PeerPresence.fromJson('lumina', {
        'status': 'online',
        'custom_message': 'In flow',
        'last_seen': '2026-03-02T10:00:00Z',
      });
      expect(presence.peerId, 'lumina');
      expect(presence.status, 'online');
      expect(presence.customMessage, 'In flow');
      expect(presence.lastSeen, isNotNull);
    });

    test('defaults status to offline', () {
      final presence = PeerPresence.fromJson('jarvis', {});
      expect(presence.status, 'offline');
    });
  });

  group('peerPresenceProvider', () {
    late MockSKCommClient mockClient;
    late ProviderContainer container;

    setUp(() {
      mockClient = MockSKCommClient();
      container = ProviderContainer(
        overrides: [skcommClientProvider.overrideWithValue(mockClient)],
      );
    });

    tearDown(() => container.dispose());

    test('returns offline presence when daemon is unreachable', () async {
      when(() => mockClient.getPeerPresence(any()))
          .thenThrow(SKCommException('timeout'));

      final presence =
          await container.read(peerPresenceProvider('lumina').future);
      expect(presence.peerId, 'lumina');
      expect(presence.status, 'offline');
    });

    test('returns real presence when daemon responds', () async {
      when(() => mockClient.getPeerPresence('jarvis')).thenAnswer(
        (_) async => {'status': 'online', 'custom_message': 'Patrolling'},
      );

      final presence =
          await container.read(peerPresenceProvider('jarvis').future);
      expect(presence.status, 'online');
      expect(presence.customMessage, 'Patrolling');
    });

    test('isolates per peerId', () async {
      when(() => mockClient.getPeerPresence('a'))
          .thenAnswer((_) async => {'status': 'online'});
      when(() => mockClient.getPeerPresence('b'))
          .thenAnswer((_) async => {'status': 'away'});

      final a = await container.read(peerPresenceProvider('a').future);
      final b = await container.read(peerPresenceProvider('b').future);
      expect(a.status, 'online');
      expect(b.status, 'away');
    });
  });

  group('LocalPresenceNotifier', () {
    late MockSKCommClient mockClient;
    late ProviderContainer container;

    setUp(() {
      mockClient = MockSKCommClient();
      container = ProviderContainer(
        overrides: [skcommClientProvider.overrideWithValue(mockClient)],
      );
    });

    tearDown(() => container.dispose());

    test('initial state is offline and not broadcasting', () {
      final state = container.read(localPresenceProvider);
      expect(state.status, 'offline');
      expect(state.isBroadcasting, isFalse);
    });

    test('broadcast updates state on success', () async {
      when(() => mockClient.broadcastPresence(
            status: any(named: 'status'),
            customMessage: any(named: 'customMessage'),
          )).thenAnswer((_) async {});

      await container
          .read(localPresenceProvider.notifier)
          .broadcast(status: 'online', customMessage: 'Ready');

      final state = container.read(localPresenceProvider);
      expect(state.status, 'online');
      expect(state.customMessage, 'Ready');
      expect(state.isBroadcasting, isFalse);
    });

    test('broadcast is silent when daemon is offline', () async {
      when(() => mockClient.broadcastPresence(
            status: any(named: 'status'),
            customMessage: any(named: 'customMessage'),
          )).thenThrow(SKCommException('timeout'));

      // Should not throw.
      await container
          .read(localPresenceProvider.notifier)
          .broadcast(status: 'online');

      final state = container.read(localPresenceProvider);
      expect(state.isBroadcasting, isFalse);
      // Status unchanged (still 'offline') since broadcast failed.
      expect(state.status, 'offline');
    });
  });
}
