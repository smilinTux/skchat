import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mocktail/mocktail.dart';

import 'package:skchat_mobile/core/providers/trust_provider.dart';
import 'package:skchat_mobile/core/transport/skcomm_client.dart';

class MockSKCommClient extends Mock implements SKCommClient {}

void main() {
  group('TrustInfo.fromJson', () {
    test('maps all fields', () {
      final info = TrustInfo.fromJson('jarvis', {
        'fingerprint': 'abc123',
        'trust_level': 'high',
        'trust_score': 0.95,
        'verified': true,
        'first_seen': '2026-01-01T00:00:00Z',
        'last_seen': '2026-03-02T10:00:00Z',
      });
      expect(info.peerId, 'jarvis');
      expect(info.fingerprint, 'abc123');
      expect(info.trustLevel, 'high');
      expect(info.trustScore, closeTo(0.95, 0.001));
      expect(info.verified, isTrue);
      expect(info.firstSeen, isNotNull);
    });

    test('falls back to unknown trust level', () {
      final info = TrustInfo.fromJson('lumina', {});
      expect(info.trustLevel, 'unknown');
      expect(info.verified, isFalse);
    });

    test('uses level field when trust_level absent', () {
      final info =
          TrustInfo.fromJson('opus', {'level': 'medium', 'score': 0.7});
      expect(info.trustLevel, 'medium');
      expect(info.trustScore, closeTo(0.7, 0.001));
    });
  });

  group('trustProvider', () {
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

    test('returns TrustInfo.unknown when daemon is unreachable', () async {
      when(() => mockClient.getTrustInfo(any()))
          .thenThrow(SKCommException('offline'));

      final trust =
          await container.read(trustProvider('jarvis').future);
      expect(trust.peerId, 'jarvis');
      expect(trust.trustLevel, 'unknown');
      expect(trust.verified, isFalse);
    });

    test('returns parsed trust when daemon responds', () async {
      when(() => mockClient.getTrustInfo('lumina')).thenAnswer((_) async => {
            'fingerprint': 'fp-lumina',
            'trust_level': 'high',
            'trust_score': 0.9,
            'verified': true,
          });

      final trust = await container.read(trustProvider('lumina').future);
      expect(trust.peerId, 'lumina');
      expect(trust.trustLevel, 'high');
      expect(trust.verified, isTrue);
    });

    test('isolates family instances per peerId', () async {
      when(() => mockClient.getTrustInfo('a'))
          .thenAnswer((_) async => {'trust_level': 'high'});
      when(() => mockClient.getTrustInfo('b'))
          .thenAnswer((_) async => {'trust_level': 'low'});

      final trustA = await container.read(trustProvider('a').future);
      final trustB = await container.read(trustProvider('b').future);

      expect(trustA.trustLevel, 'high');
      expect(trustB.trustLevel, 'low');
    });
  });
}
