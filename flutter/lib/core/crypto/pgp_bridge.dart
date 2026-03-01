import 'dart:convert';
import 'dart:isolate';
import 'dart:math';
import 'dart:typed_data';

import 'package:asn1lib/asn1lib.dart';
import 'package:pointycastle/export.dart';

/// RSA keypair with a PGP-style SHA-1 fingerprint.
class PgpKeyPair {
  const PgpKeyPair({
    required this.fingerprint,
    required this.publicKeyPem,
    required this.privateKeyPem,
  });

  /// 40-hex-char fingerprint formatted as five groups of four, split at the
  /// midpoint with a double space — matching OpenPGP display convention.
  final String fingerprint;

  /// PKCS#1 PEM-encoded RSA public key.
  final String publicKeyPem;

  /// PKCS#1 PEM-encoded RSA private key.  Keep this secret and store it
  /// in secure storage (e.g. flutter_secure_storage) before shipping.
  final String privateKeyPem;
}

/// Thin Dart wrapper around RSA keygen / sign / verify / encrypt primitives.
///
/// Uses [pointycastle] for pure-Dart cryptography so no platform-channel is
/// required.  Full OpenPGP packet formatting (UID, subkeys, self-signatures)
/// is deferred to a native/FFI layer; this bridge provides the key material
/// and fingerprint consumed by the onboarding flow and [PairPage].
///
/// ## Typical usage
/// ```dart
/// final keyPair = await PgpBridge.generateKeyPair();
/// final sig = await PgpBridge.signAsync(payload, keyPair.privateKeyPem);
/// final ok  = await PgpBridge.verifyAsync(payload, sig, keyPair.publicKeyPem);
/// ```
///
/// All heavy operations provide both synchronous and async (isolate-backed)
/// variants.  Prefer the `*Async` methods from the UI layer to avoid jank.
class PgpBridge {
  PgpBridge._();

  // ── Key generation ──────────────────────────────────────────────────────

  /// Generate a fresh RSA-[bits] keypair and return it with a fingerprint.
  ///
  /// RSA keygen is CPU-intensive (~1-3 s for 2048 bits on mobile).
  /// Runs in a background isolate to avoid blocking the UI thread.
  static Future<PgpKeyPair> generateKeyPair({int bits = 2048}) async {
    return Isolate.run(() => _generateKeyPairSync(bits: bits));
  }

  /// Synchronous key generation (runs inside an isolate).
  static PgpKeyPair _generateKeyPairSync({int bits = 2048}) {
    final secureRandom = _buildSecureRandom();
    final keyGen = RSAKeyGenerator()
      ..init(
        ParametersWithRandom(
          RSAKeyGeneratorParameters(BigInt.from(65537), bits, 64),
          secureRandom,
        ),
      );

    final pair = keyGen.generateKeyPair();
    final pub = pair.publicKey as RSAPublicKey;
    final priv = pair.privateKey as RSAPrivateKey;

    final pubDer = _encodePublicKeyDer(pub);
    final privDer = _encodePrivateKeyDer(priv);

    return PgpKeyPair(
      fingerprint: _computeFingerprint(pubDer),
      publicKeyPem: _toPem('RSA PUBLIC KEY', pubDer),
      privateKeyPem: _toPem('RSA PRIVATE KEY', privDer),
    );
  }

  // ── Sign ────────────────────────────────────────────────────────────────

  /// Sign UTF-8 [data] with [privateKeyPem] using PKCS#1 v1.5 + SHA-256.
  ///
  /// Returns a base64-encoded signature.
  /// Runs in a background isolate to avoid blocking the UI thread.
  static Future<String> signAsync(String data, String privateKeyPem) {
    return Isolate.run(() => sign(data, privateKeyPem));
  }

  /// Synchronous sign -- prefer [signAsync] from the UI layer.
  static String sign(String data, String privateKeyPem) {
    final key = _parsePrivateKey(privateKeyPem);
    // DigestInfo header for SHA-256 (RFC 3447, Appendix B.1).
    const sha256DigestInfo = '3031300d060960864801650304020105000420';
    final signer = RSASigner(SHA256Digest(), sha256DigestInfo)
      ..init(true, PrivateKeyParameter<RSAPrivateKey>(key));
    final sig = signer.generateSignature(
      Uint8List.fromList(utf8.encode(data)),
    ) as RSASignature;
    return base64.encode(sig.bytes);
  }

  // ── Verify ──────────────────────────────────────────────────────────────

  /// Verify a base64 [signature] over UTF-8 [data] using [publicKeyPem].
  ///
  /// Returns `false` on any error (invalid key, tampered data, bad padding).
  /// Runs in a background isolate to avoid blocking the UI thread.
  static Future<bool> verifyAsync(
      String data, String signature, String publicKeyPem) {
    return Isolate.run(() => verify(data, signature, publicKeyPem));
  }

  /// Synchronous verify -- prefer [verifyAsync] from the UI layer.
  static bool verify(String data, String signature, String publicKeyPem) {
    final key = _parsePublicKey(publicKeyPem);
    const sha256DigestInfo = '3031300d060960864801650304020105000420';
    final verifier = RSASigner(SHA256Digest(), sha256DigestInfo)
      ..init(false, PublicKeyParameter<RSAPublicKey>(key));
    try {
      return verifier.verifySignature(
        Uint8List.fromList(utf8.encode(data)),
        RSASignature(base64.decode(signature)),
      );
    } catch (_) {
      return false;
    }
  }

  // ── Encrypt ─────────────────────────────────────────────────────────────

  /// Encrypt [plaintext] for the owner of [publicKeyPem] using RSA-OAEP.
  ///
  /// Returns base64 ciphertext.  Note: RSA encryption is limited to
  /// `(keyBits / 8) - 42` bytes of plaintext; use hybrid encryption for
  /// larger payloads.
  /// Runs in a background isolate to avoid blocking the UI thread.
  static Future<String> encryptAsync(String plaintext, String publicKeyPem) {
    return Isolate.run(() => encrypt(plaintext, publicKeyPem));
  }

  /// Synchronous encrypt -- prefer [encryptAsync] from the UI layer.
  static String encrypt(String plaintext, String publicKeyPem) {
    final key = _parsePublicKey(publicKeyPem);
    final cipher = OAEPEncoding(RSAEngine())
      ..init(true, PublicKeyParameter<RSAPublicKey>(key));
    final out = cipher.process(Uint8List.fromList(utf8.encode(plaintext)));
    return base64.encode(out);
  }

  // ── Decrypt ─────────────────────────────────────────────────────────────

  /// Decrypt base64 [ciphertext] using [privateKeyPem].
  /// Runs in a background isolate to avoid blocking the UI thread.
  static Future<String> decryptAsync(
      String ciphertext, String privateKeyPem) {
    return Isolate.run(() => decrypt(ciphertext, privateKeyPem));
  }

  /// Synchronous decrypt -- prefer [decryptAsync] from the UI layer.
  static String decrypt(String ciphertext, String privateKeyPem) {
    final key = _parsePrivateKey(privateKeyPem);
    final cipher = OAEPEncoding(RSAEngine())
      ..init(false, PrivateKeyParameter<RSAPrivateKey>(key));
    final out = cipher.process(base64.decode(ciphertext));
    return utf8.decode(out);
  }

  // ── Import ──────────────────────────────────────────────────────────────

  /// Reconstruct a [PgpKeyPair] from a PKCS#1 PEM-encoded RSA private key.
  ///
  /// The private key already contains the public parameters (n, e), so this
  /// re-encodes the public key and recomputes the fingerprint — no separate
  /// public key file is needed.
  ///
  /// Throws [FormatException] if [privateKeyPem] cannot be parsed.
  static PgpKeyPair importPrivateKey(String privateKeyPem) {
    final priv = _parsePrivateKey(privateKeyPem);
    final pub = RSAPublicKey(priv.modulus!, priv.publicExponent!);
    final pubDer = _encodePublicKeyDer(pub);
    return PgpKeyPair(
      fingerprint: _computeFingerprint(pubDer),
      publicKeyPem: _toPem('RSA PUBLIC KEY', pubDer),
      privateKeyPem: privateKeyPem,
    );
  }

  // ── Internals ────────────────────────────────────────────────────────────

  /// Fortuna PRNG seeded from [Random.secure].
  static SecureRandom _buildSecureRandom() {
    final rng = Random.secure();
    final seed = Uint8List(32);
    for (var i = 0; i < seed.length; i++) {
      seed[i] = rng.nextInt(256);
    }
    return FortunaRandom()..seed(KeyParameter(seed));
  }

  /// PKCS#1 RSAPublicKey DER (RFC 3447 Appendix A.1.1).
  static Uint8List _encodePublicKeyDer(RSAPublicKey key) {
    final seq = ASN1Sequence()
      ..add(ASN1Integer(key.modulus!))
      ..add(ASN1Integer(key.exponent!));
    return seq.encodedBytes;
  }

  /// PKCS#1 RSAPrivateKey DER (RFC 3447 Appendix A.1.2).
  static Uint8List _encodePrivateKeyDer(RSAPrivateKey key) {
    final p = key.p!;
    final q = key.q!;
    final d = key.privateExponent!;
    final dp = d % (p - BigInt.one);
    final dq = d % (q - BigInt.one);
    final qInv = q.modInverse(p);
    final seq = ASN1Sequence()
      ..add(ASN1Integer(BigInt.zero)) // version = 0
      ..add(ASN1Integer(key.modulus!))
      ..add(ASN1Integer(key.publicExponent!))
      ..add(ASN1Integer(d))
      ..add(ASN1Integer(p))
      ..add(ASN1Integer(q))
      ..add(ASN1Integer(dp))
      ..add(ASN1Integer(dq))
      ..add(ASN1Integer(qInv));
    return seq.encodedBytes;
  }

  /// SHA-1 fingerprint over DER bytes, formatted as `AAAA BBBB … JJJJ` with
  /// a double space separating the two halves (10 groups of 4 hex chars).
  static String _computeFingerprint(Uint8List der) {
    final digest = SHA1Digest();
    final hash = Uint8List(digest.digestSize);
    digest.update(der, 0, der.length);
    digest.doFinal(hash, 0);

    final hex = hash
        .map((b) => b.toRadixString(16).padLeft(2, '0').toUpperCase())
        .join();
    final groups = [
      for (var i = 0; i < hex.length; i += 4)
        hex.substring(i, (i + 4).clamp(0, hex.length)),
    ];
    return '${groups.sublist(0, 5).join(' ')}  ${groups.sublist(5).join(' ')}';
  }

  static String _toPem(String label, Uint8List der) {
    final b64 = base64.encode(der);
    final sb = StringBuffer('-----BEGIN $label-----\n');
    for (var i = 0; i < b64.length; i += 64) {
      sb
        ..write(b64.substring(i, (i + 64).clamp(0, b64.length)))
        ..write('\n');
    }
    sb.write('-----END $label-----');
    return sb.toString();
  }

  static RSAPublicKey _parsePublicKey(String pem) {
    final b64 = pem
        .replaceAll(RegExp(r'-----[^-]+-----'), '')
        .replaceAll(RegExp(r'\s'), '');
    final seq =
        ASN1Parser(base64.decode(b64)).nextObject() as ASN1Sequence;
    return RSAPublicKey(
      (seq.elements[0] as ASN1Integer).valueAsBigInteger,
      (seq.elements[1] as ASN1Integer).valueAsBigInteger,
    );
  }

  static RSAPrivateKey _parsePrivateKey(String pem) {
    final b64 = pem
        .replaceAll(RegExp(r'-----[^-]+-----'), '')
        .replaceAll(RegExp(r'\s'), '');
    final seq =
        ASN1Parser(base64.decode(b64)).nextObject() as ASN1Sequence;
    final e = seq.elements;
    // Indices per RFC 3447 A.1.2: 0=version, 1=n, 2=e, 3=d, 4=p, 5=q, …
    return RSAPrivateKey(
      (e[1] as ASN1Integer).valueAsBigInteger, // modulus
      (e[3] as ASN1Integer).valueAsBigInteger, // privateExponent
      (e[4] as ASN1Integer).valueAsBigInteger, // prime1 (p)
      (e[5] as ASN1Integer).valueAsBigInteger, // prime2 (q)
    );
  }
}
