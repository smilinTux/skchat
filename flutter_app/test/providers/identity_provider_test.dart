import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mocktail/mocktail.dart';

import 'package:skchat_mobile/core/providers/identity_provider.dart';
import 'package:skchat_mobile/core/transport/skcomm_client.dart';

class MockSKCommClient extends Mock implements SKCommClient {}

void main() {
  group('IdentityInfo.fromJson', () {
    test('maps agent_name field', () {
      final info = IdentityInfo.fromJson({
        'agent_name': 'opus',
        'fingerprint': 'deadbeef',
        'conscious': true,
      });
      expect(info.agentName, 'opus');
      expect(info.fingerprint, 'deadbeef');
      expect(info.conscious, isTrue);
    });

    test('falls back to name field', () {
      final info = IdentityInfo.fromJson({'name': 'lumina'});
      expect(info.agentName, 'lumina');
    });

    test('returns unknown for empty json', () {
      final info = IdentityInfo.fromJson({});
      expect(info.agentName, 'unknown');
      expect(info.conscious, isFalse);
    });
  });

  group('identityProvider', () {
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

    test('returns IdentityInfo.unknown when daemon is unreachable', () async {
      when(() => mockClient.getIdentity())
          .thenThrow(SKCommException('offline'));

      final identity = await container.read(identityProvider.future);
      expect(identity.agentName, 'unknown');
    });

    test('returns parsed identity when daemon responds', () async {
      when(() => mockClient.getIdentity()).thenAnswer((_) async => {
            'agent_name': 'opus',
            'fingerprint': '6136E987',
            'active_soul': 'lumina',
            'conscious': true,
          });

      final identity = await container.read(identityProvider.future);
      expect(identity.agentName, 'opus');
      expect(identity.fingerprint, '6136E987');
      expect(identity.activeSoul, 'lumina');
      expect(identity.conscious, isTrue);
    });

    test('handles partial response gracefully', () async {
      when(() => mockClient.getIdentity())
          .thenAnswer((_) async => {'name': 'jarvis'});

      final identity = await container.read(identityProvider.future);
      expect(identity.agentName, 'jarvis');
      expect(identity.fingerprint, isNull);
    });
  });
}
