import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../transport/skcomm_client.dart';

/// Presence data for a specific peer.
class PeerPresence {
  final String peerId;
  final String status;
  final String? customMessage;
  final DateTime? lastSeen;

  const PeerPresence({
    required this.peerId,
    required this.status,
    this.customMessage,
    this.lastSeen,
  });

  factory PeerPresence.fromJson(String peerId, Map<String, dynamic> json) {
    return PeerPresence(
      peerId: peerId,
      status: json['status'] as String? ?? 'offline',
      customMessage: json['custom_message'] as String?,
      lastSeen: json['last_seen'] != null
          ? DateTime.tryParse(json['last_seen'] as String)
          : null,
    );
  }

  static PeerPresence offline(String peerId) =>
      PeerPresence(peerId: peerId, status: 'offline');
}

/// Provider that fetches presence info for a specific peer.
/// Falls back to offline when the daemon is unreachable.
final peerPresenceProvider =
    FutureProvider.family<PeerPresence, String>((ref, peerId) async {
  final client = ref.read(skcommClientProvider);
  try {
    final raw = await client.getPeerPresence(peerId);
    return PeerPresence.fromJson(peerId, raw);
  } catch (_) {
    return PeerPresence.offline(peerId);
  }
});

/// State for the local presence broadcaster.
class LocalPresenceState {
  final String status;
  final String? customMessage;
  final bool isBroadcasting;

  const LocalPresenceState({
    this.status = 'offline',
    this.customMessage,
    this.isBroadcasting = false,
  });

  LocalPresenceState copyWith({
    String? status,
    String? customMessage,
    bool? isBroadcasting,
  }) {
    return LocalPresenceState(
      status: status ?? this.status,
      customMessage: customMessage ?? this.customMessage,
      isBroadcasting: isBroadcasting ?? this.isBroadcasting,
    );
  }
}

/// Notifier that manages broadcasting local presence to the daemon.
class LocalPresenceNotifier extends Notifier<LocalPresenceState> {
  @override
  LocalPresenceState build() => const LocalPresenceState();

  /// Broadcast [status] to the SKComm daemon.
  Future<void> broadcast({
    required String status,
    String? customMessage,
  }) async {
    state = state.copyWith(isBroadcasting: true);
    final client = ref.read(skcommClientProvider);
    try {
      await client.broadcastPresence(
        status: status,
        customMessage: customMessage,
      );
      state = state.copyWith(
        status: status,
        customMessage: customMessage,
        isBroadcasting: false,
      );
    } catch (_) {
      // Daemon offline — keep previous state, stop spinner.
      state = state.copyWith(isBroadcasting: false);
    }
  }
}

/// Provider for broadcasting local presence status.
final localPresenceProvider =
    NotifierProvider<LocalPresenceNotifier, LocalPresenceState>(
  LocalPresenceNotifier.new,
);
