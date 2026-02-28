import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../services/biometric_service.dart';
import '../../services/capauth_service.dart';

// ── State ─────────────────────────────────────────────────────────────────

enum CapAuthStatus { idle, authenticating, authenticated, error }

class CapAuthState {
  const CapAuthState({
    this.status = CapAuthStatus.idle,
    this.session,
    this.error,
    this.biometricUnlocked = false,
  });

  final CapAuthStatus status;

  /// The active session, present when [status] == [CapAuthStatus.authenticated].
  final CapAuthSession? session;

  /// Error message set when [status] == [CapAuthStatus.error].
  final String? error;

  /// True after the user has passed biometric auth in this app session.
  /// Cleared when the notifier rebuilds (i.e. on cold start).
  final bool biometricUnlocked;

  bool get isAuthenticated =>
      status == CapAuthStatus.authenticated && session != null;

  CapAuthState copyWith({
    CapAuthStatus? status,
    CapAuthSession? session,
    String? error,
    bool? biometricUnlocked,
  }) =>
      CapAuthState(
        status: status ?? this.status,
        session: session ?? this.session,
        error: error,
        biometricUnlocked: biometricUnlocked ?? this.biometricUnlocked,
      );
}

// ── Notifier ──────────────────────────────────────────────────────────────

class CapAuthNotifier extends Notifier<CapAuthState> {
  @override
  CapAuthState build() {
    Future.microtask(_restoreSessions);
    return const CapAuthState();
  }

  Future<void> _restoreSessions() async {
    final service = ref.read(capAuthServiceProvider);
    final sessions = await service.loadAllSessions();
    if (sessions.isNotEmpty) {
      state = state.copyWith(
        status: CapAuthStatus.authenticated,
        session: sessions.first,
      );
    }
  }

  // ── Biometric ────────────────────────────────────────────────────────────

  /// Gate: prompt biometric auth, update [biometricUnlocked] on success.
  Future<bool> requestBiometricUnlock() async {
    final biometric = ref.read(biometricServiceProvider);
    final ok = await biometric.authenticate(
      reason: 'Unlock sovereign identity to sign CapAuth challenge',
    );
    if (ok) {
      state = state.copyWith(biometricUnlocked: true);
    }
    return ok;
  }

  // ── Login ─────────────────────────────────────────────────────────────────

  /// Full login from a scanned QR payload.
  ///
  /// 1. Ensures biometric is unlocked (prompts if needed).
  /// 2. Signs the nonce via the local SKComm daemon.
  /// 3. Verifies with the CapAuth server.
  /// 4. Caches the returned session token.
  ///
  /// Returns true on success.
  Future<bool> loginWithQr(CapAuthQrPayload payload) async {
    if (!state.biometricUnlocked) {
      final unlocked = await requestBiometricUnlock();
      if (!unlocked) return false;
    }

    state = state.copyWith(status: CapAuthStatus.authenticating, error: null);
    try {
      final service = ref.read(capAuthServiceProvider);
      final session = await service.login(payload);
      state = state.copyWith(
        status: CapAuthStatus.authenticated,
        session: session,
        error: null,
      );
      return true;
    } catch (e) {
      state = state.copyWith(
        status: CapAuthStatus.error,
        error: _friendlyError(e),
      );
      return false;
    }
  }

  // ── Logout ────────────────────────────────────────────────────────────────

  Future<void> logout() async {
    final server = state.session?.server;
    if (server != null) {
      await ref.read(capAuthServiceProvider).clearSession(server);
    }
    state = const CapAuthState();
  }

  // ── Helpers ───────────────────────────────────────────────────────────────

  String _friendlyError(Object e) {
    final msg = e.toString();
    if (msg.contains('SocketException') || msg.contains('connection')) {
      return 'Cannot reach server. Is the SKComm daemon running?';
    }
    if (msg.contains('401') || msg.contains('403')) {
      return 'Server rejected the signature. Check your identity key.';
    }
    return msg;
  }
}

// ── Providers ─────────────────────────────────────────────────────────────

final capAuthProvider =
    NotifierProvider<CapAuthNotifier, CapAuthState>(CapAuthNotifier.new);

/// Convenience provider — true while the user has an active session.
final isCapAuthenticatedProvider = Provider<bool>((ref) {
  return ref.watch(capAuthProvider).isAuthenticated;
});
