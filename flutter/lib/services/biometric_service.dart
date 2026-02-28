import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:local_auth/local_auth.dart';

/// Wraps `local_auth` for biometric-gated operations such as signing
/// CapAuth challenges with the sovereign PGP key.
class BiometricService {
  final LocalAuthentication _auth = LocalAuthentication();

  /// True if the device can perform biometric or device-credential auth.
  Future<bool> isAvailable() async {
    try {
      final canCheck = await _auth.canCheckBiometrics;
      final deviceSupported = await _auth.isDeviceSupported();
      return canCheck || deviceSupported;
    } catch (_) {
      return false;
    }
  }

  /// Returns the enrolled biometric types (fingerprint, face, iris, etc.).
  Future<List<BiometricType>> availableTypes() async {
    try {
      return await _auth.getAvailableBiometrics();
    } catch (_) {
      return [];
    }
  }

  /// Prompt for biometric / device-credential authentication.
  ///
  /// [reason] is shown in the system prompt.
  /// Returns true on success, false on failure or user cancellation.
  Future<bool> authenticate({
    String reason = 'Unlock sovereign identity',
  }) async {
    try {
      return await _auth.authenticate(
        localizedReason: reason,
        options: const AuthenticationOptions(
          // Allow PIN/pattern fallback so users without biometrics can still auth.
          biometricOnly: false,
          stickyAuth: true,
        ),
      );
    } catch (_) {
      return false;
    }
  }
}

// ── Provider ──────────────────────────────────────────────────────────────

final biometricServiceProvider = Provider<BiometricService>((ref) {
  return BiometricService();
});
