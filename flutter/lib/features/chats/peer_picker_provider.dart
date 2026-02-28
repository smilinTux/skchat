import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../services/skcomm_client.dart';

/// Loads available peers from the SKComm daemon for the peer picker.
/// Returns [PeerInfo] objects sorted alphabetically with online peers first.
class PeerPickerNotifier extends AsyncNotifier<List<PeerInfo>> {
  @override
  Future<List<PeerInfo>> build() => _fetchPeers();

  Future<List<PeerInfo>> _fetchPeers() async {
    final client = ref.read(skcommClientProvider);
    final alive = await client.isAlive();
    if (!alive) throw Exception('SKComm daemon is offline');

    final peers = await client.getPeers();

    // Deduplicate by lowercase name.
    final seen = <String>{};
    final unique = <PeerInfo>[];
    for (final p in peers) {
      final key = p.name.toLowerCase();
      if (seen.add(key)) unique.add(p);
    }

    // Sort: online first, then alphabetically.
    unique.sort((a, b) {
      final aOnline = isOnline(a) ? 0 : 1;
      final bOnline = isOnline(b) ? 0 : 1;
      if (aOnline != bOnline) return aOnline.compareTo(bOnline);
      return a.name.toLowerCase().compareTo(b.name.toLowerCase());
    });

    return unique;
  }

  Future<void> refresh() async {
    state = const AsyncLoading();
    state = await AsyncValue.guard(_fetchPeers);
  }

  static bool isOnline(PeerInfo peer) {
    if (peer.lastSeen == null) return false;
    return DateTime.now().difference(peer.lastSeen!).inMinutes < 30;
  }
}

final peerPickerProvider =
    AsyncNotifierProvider<PeerPickerNotifier, List<PeerInfo>>(
  PeerPickerNotifier.new,
);
