import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../transport/skcomm_client.dart';

/// Local agent identity information.
class IdentityInfo {
  final String agentName;
  final String? fingerprint;
  final String? publicKey;
  final String? activeSoul;
  final bool conscious;

  const IdentityInfo({
    required this.agentName,
    this.fingerprint,
    this.publicKey,
    this.activeSoul,
    this.conscious = false,
  });

  factory IdentityInfo.fromJson(Map<String, dynamic> json) {
    return IdentityInfo(
      agentName: json['agent_name'] as String? ??
          json['name'] as String? ??
          'unknown',
      fingerprint: json['fingerprint'] as String?,
      publicKey: json['public_key'] as String?,
      activeSoul: json['active_soul'] as String?,
      conscious: json['conscious'] as bool? ?? false,
    );
  }

  static const unknown = IdentityInfo(agentName: 'unknown');
}

/// Provider that fetches the local identity from the SKComm daemon.
/// Falls back to [IdentityInfo.unknown] when the daemon is unreachable.
final identityProvider = FutureProvider<IdentityInfo>((ref) async {
  final client = ref.read(skcommClientProvider);
  try {
    final raw = await client.getIdentity();
    return IdentityInfo.fromJson(raw);
  } catch (_) {
    return IdentityInfo.unknown;
  }
});
