import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mocktail/mocktail.dart';
import 'package:skchat/features/chats/peer_picker_provider.dart';
import 'package:skchat/services/skcomm_client.dart';

class MockSKCommClient extends Mock implements SKCommClient {}

void main() {
  late MockSKCommClient mockClient;

  setUp(() {
    mockClient = MockSKCommClient();
  });

  ProviderContainer createContainer() {
    return ProviderContainer(
      overrides: [
        skcommClientProvider.overrideWithValue(mockClient),
      ],
    );
  }

  group('PeerPickerNotifier', () {
    test('throws when daemon is offline', () async {
      when(() => mockClient.isAlive()).thenAnswer((_) async => false);

      final container = createContainer();

      // Wait for the async build to complete.
      await expectLater(
        container.read(peerPickerProvider.future),
        throwsA(isA<Exception>()),
      );

      container.dispose();
    });

    test('returns peers sorted online-first then alphabetically', () async {
      final now = DateTime.now();
      when(() => mockClient.isAlive()).thenAnswer((_) async => true);
      when(() => mockClient.getPeers()).thenAnswer((_) async => [
            PeerInfo(
              name: 'Ava',
              lastSeen: now.subtract(const Duration(hours: 2)),
            ), // offline
            PeerInfo(
              name: 'Lumina',
              lastSeen: now.subtract(const Duration(minutes: 5)),
            ), // online
            PeerInfo(
              name: 'Jarvis',
              lastSeen: now.subtract(const Duration(minutes: 1)),
            ), // online
          ]);

      final container = createContainer();
      final peers = await container.read(peerPickerProvider.future);

      // Online peers first (Jarvis, Lumina alphabetically), then offline (Ava).
      expect(peers.length, 3);
      expect(peers[0].name, 'Jarvis');
      expect(peers[1].name, 'Lumina');
      expect(peers[2].name, 'Ava');

      container.dispose();
    });

    test('deduplicates peers by lowercase name', () async {
      final now = DateTime.now();
      when(() => mockClient.isAlive()).thenAnswer((_) async => true);
      when(() => mockClient.getPeers()).thenAnswer((_) async => [
            PeerInfo(
              name: 'Lumina',
              lastSeen: now.subtract(const Duration(minutes: 5)),
            ),
            PeerInfo(
              name: 'lumina',
              lastSeen: now.subtract(const Duration(minutes: 10)),
            ),
          ]);

      final container = createContainer();
      final peers = await container.read(peerPickerProvider.future);

      expect(peers.length, 1);
      expect(peers.first.name, 'Lumina');

      container.dispose();
    });

    test('returns empty list when daemon has no peers', () async {
      when(() => mockClient.isAlive()).thenAnswer((_) async => true);
      when(() => mockClient.getPeers()).thenAnswer((_) async => []);

      final container = createContainer();
      final peers = await container.read(peerPickerProvider.future);

      expect(peers, isEmpty);

      container.dispose();
    });
  });

  group('PeerPickerNotifier.isOnline', () {
    test('returns true for peer seen within 30 minutes', () {
      final peer = PeerInfo(
        name: 'test',
        lastSeen: DateTime.now().subtract(const Duration(minutes: 10)),
      );
      expect(PeerPickerNotifier.isOnline(peer), true);
    });

    test('returns false for peer seen over 30 minutes ago', () {
      final peer = PeerInfo(
        name: 'test',
        lastSeen: DateTime.now().subtract(const Duration(minutes: 31)),
      );
      expect(PeerPickerNotifier.isOnline(peer), false);
    });

    test('returns false for peer with no lastSeen', () {
      const peer = PeerInfo(name: 'test');
      expect(PeerPickerNotifier.isOnline(peer), false);
    });
  });
}
