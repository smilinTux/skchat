import 'package:flutter_test/flutter_test.dart';
import 'package:skchat/services/skcomm_sync.dart';

void main() {
  group('DaemonStatus', () {
    test('has all expected values', () {
      expect(DaemonStatus.values.length, 4);
      expect(DaemonStatus.values, contains(DaemonStatus.connecting));
      expect(DaemonStatus.values, contains(DaemonStatus.online));
      expect(DaemonStatus.values, contains(DaemonStatus.offline));
      expect(DaemonStatus.values, contains(DaemonStatus.error));
    });
  });

  group('DaemonState', () {
    test('defaults to connecting status', () {
      const state = DaemonState();
      expect(state.status, DaemonStatus.connecting);
      expect(state.errorMessage, isNull);
      expect(state.lastPollAt, isNull);
      expect(state.transportInfo, isNull);
    });

    test('copyWith preserves existing values', () {
      final state = DaemonState(
        status: DaemonStatus.online,
        lastPollAt: DateTime(2026, 2, 28),
        transportInfo: {'syncthing': true},
      );

      final updated = state.copyWith(status: DaemonStatus.offline);

      expect(updated.status, DaemonStatus.offline);
      expect(updated.lastPollAt, state.lastPollAt);
      // Note: errorMessage is explicitly set to null in copyWith.
    });

    test('copyWith updates status to online', () {
      const state = DaemonState(status: DaemonStatus.connecting);

      final updated = state.copyWith(
        status: DaemonStatus.online,
        lastPollAt: DateTime(2026, 2, 28, 14, 30),
        transportInfo: {'syncthing': true, 'nostr': false},
      );

      expect(updated.status, DaemonStatus.online);
      expect(updated.lastPollAt?.hour, 14);
      expect(updated.transportInfo?['syncthing'], true);
    });

    test('copyWith can set error message', () {
      const state = DaemonState();

      final updated = state.copyWith(
        status: DaemonStatus.error,
        errorMessage: 'Connection refused',
      );

      expect(updated.status, DaemonStatus.error);
      expect(updated.errorMessage, 'Connection refused');
    });

    test('copyWith clears error on status change', () {
      final state = DaemonState(
        status: DaemonStatus.error,
        errorMessage: 'Previous error',
      );

      final updated = state.copyWith(status: DaemonStatus.online);

      expect(updated.status, DaemonStatus.online);
      // errorMessage is set to null unless explicitly provided.
      expect(updated.errorMessage, isNull);
    });

    test('copyWith preserves transportInfo across updates', () {
      final info = {'syncthing': true, 'peer_count': 3};
      final state = DaemonState(
        status: DaemonStatus.online,
        transportInfo: info,
      );

      final updated = state.copyWith(
        lastPollAt: DateTime(2026, 2, 28, 15, 0),
      );

      expect(updated.transportInfo, info);
    });
  });

  group('SKCommSyncNotifier provider', () {
    test('provider is accessible', () {
      expect(skcommSyncProvider, isNotNull);
    });
  });
}
