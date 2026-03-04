import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../transport/skcomm_client.dart';

/// Trust relationship info for a specific peer.
class TrustInfo {
  final String peerId;
  final String? fingerprint;
  final String trustLevel;
  final double? trustScore;
  final bool verified;
  final DateTime? firstSeen;
  final DateTime? lastSeen;

  const TrustInfo({
    required this.peerId,
    this.fingerprint,
    this.trustLevel = 'unknown',
    this.trustScore,
    this.verified = false,
    this.firstSeen,
    this.lastSeen,
  });

  factory TrustInfo.fromJson(String peerId, Map<String, dynamic> json) {
    return TrustInfo(
      peerId: peerId,
      fingerprint: json['fingerprint'] as String?,
      trustLevel: json['trust_level'] as String? ??
          json['level'] as String? ??
          'unknown',
      trustScore: (json['trust_score'] as num?)?.toDouble() ??
          (json['score'] as num?)?.toDouble(),
      verified: json['verified'] as bool? ?? false,
      firstSeen: json['first_seen'] != null
          ? DateTime.tryParse(json['first_seen'] as String)
          : null,
      lastSeen: json['last_seen'] != null
          ? DateTime.tryParse(json['last_seen'] as String)
          : null,
    );
  }

  static TrustInfo unknown(String peerId) =>
      TrustInfo(peerId: peerId, trustLevel: 'unknown');
}

/// Provider that fetches trust information for a specific peer.
/// Falls back to [TrustInfo.unknown] when the daemon is unreachable.
final trustProvider =
    FutureProvider.family<TrustInfo, String>((ref, peerId) async {
  final client = ref.read(skcommClientProvider);
  try {
    final raw = await client.getTrustInfo(peerId);
    return TrustInfo.fromJson(peerId, raw);
  } catch (_) {
    // Daemon offline or peer not found.
    return TrustInfo.unknown(peerId);
  }
});
