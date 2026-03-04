import '../../core/crypto/pgp_bridge.dart';
import 'message_envelope.dart';

/// Chat-level signing and verification built on top of [PgpBridge].
///
/// The SKComm daemon handles transport-layer encryption (PGP/OpenPGP peer
/// key exchange).  This layer adds **message-level signing** so recipients
/// can independently verify the sender's identity from the Flutter keypair,
/// irrespective of which transport carried the message.
///
/// Signing is best-effort: if the local keypair is unavailable the envelope
/// is sent unsigned rather than blocking the send.
class ChatCrypto {
  ChatCrypto._();

  /// Sign [content] with [privateKeyPem] and return a [MessageEnvelope].
  ///
  /// The signature covers the UTF-8 bytes of [content] using
  /// RSA-PKCS1v15-SHA256 (via [PgpBridge.signAsync]).
  ///
  /// If signing throws (e.g. key parse error) the envelope is returned
  /// without a signature so the message is still delivered.
  static Future<MessageEnvelope> signAndWrap({
    required String id,
    required String sender,
    required String content,
    required String privateKeyPem,
    DateTime? timestamp,
    String? threadId,
    String? inReplyTo,
  }) async {
    String? signature;
    try {
      signature = await PgpBridge.signAsync(content, privateKeyPem);
    } catch (_) {
      // Key unavailable or parse error — send unsigned.
    }
    return MessageEnvelope(
      id: id,
      sender: sender,
      payload: content,
      timestamp: timestamp ?? DateTime.now(),
      signature: signature,
      threadId: threadId,
      inReplyTo: inReplyTo,
    );
  }

  /// Verify the RSA signature on [envelope] against [publicKeyPem].
  ///
  /// Returns false if the envelope carries no signature or if verification
  /// fails (tampered payload, wrong key, etc.).
  static Future<bool> verifySignature(
    MessageEnvelope envelope,
    String publicKeyPem,
  ) async {
    final sig = envelope.signature;
    if (sig == null || sig.isEmpty) return false;
    try {
      return await PgpBridge.verifyAsync(envelope.payload, sig, publicKeyPem);
    } catch (_) {
      return false;
    }
  }

  /// Extract the plaintext content and reply metadata from [envelope].
  static ({String content, String? replyToId}) unwrap(
    MessageEnvelope envelope,
  ) {
    return (content: envelope.payload, replyToId: envelope.inReplyTo);
  }
}
