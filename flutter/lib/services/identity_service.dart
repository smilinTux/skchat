import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import '../core/crypto/pgp_bridge.dart';

const _kPublicKey = 'pgp_public_key';
const _kPrivateKey = 'pgp_private_key';
const _kFingerprint = 'pgp_fingerprint';

/// Persists and loads the local PGP identity to/from the OS keychain.
class IdentityService {
  const IdentityService(this._storage);

  final FlutterSecureStorage _storage;

  /// Load the stored keypair, or null if none exists yet.
  Future<PgpKeyPair?> load() async {
    final pub = await _storage.read(key: _kPublicKey);
    final priv = await _storage.read(key: _kPrivateKey);
    final fp = await _storage.read(key: _kFingerprint);
    if (pub == null || priv == null || fp == null) return null;
    return PgpKeyPair(fingerprint: fp, publicKeyPem: pub, privateKeyPem: priv);
  }

  /// Persist [pair] to secure storage.
  Future<void> save(PgpKeyPair pair) async {
    await _storage.write(key: _kPublicKey, value: pair.publicKeyPem);
    await _storage.write(key: _kPrivateKey, value: pair.privateKeyPem);
    await _storage.write(key: _kFingerprint, value: pair.fingerprint);
  }

  /// Returns true if a key has been stored.
  Future<bool> hasKey() async {
    final fp = await _storage.read(key: _kFingerprint);
    return fp != null;
  }

  /// Delete all stored key material.
  Future<void> clear() async {
    await _storage.delete(key: _kPublicKey);
    await _storage.delete(key: _kPrivateKey);
    await _storage.delete(key: _kFingerprint);
  }
}

// ── Riverpod wiring ──────────────────────────────────────────────────────────

final _secureStorageProvider = Provider<FlutterSecureStorage>(
  (_) => const FlutterSecureStorage(
    aOptions: AndroidOptions(encryptedSharedPreferences: true),
  ),
);

final identityServiceProvider = Provider<IdentityService>(
  (ref) => IdentityService(ref.watch(_secureStorageProvider)),
);

/// Async provider that resolves to the loaded keypair (null = no key yet).
///
/// Eagerly initialised in [main] so the key is available before any screen
/// renders.
final identityKeyPairProvider =
    FutureProvider<PgpKeyPair?>((ref) async {
  return ref.watch(identityServiceProvider).load();
});
