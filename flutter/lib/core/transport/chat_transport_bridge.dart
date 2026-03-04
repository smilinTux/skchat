import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../models/chat_message.dart';
import '../../services/identity_service.dart';
import '../../services/skcomm_client.dart';
import 'chat_crypto.dart';
import 'message_envelope.dart';

/// Coordinates the full P2P send/receive pipeline between the Flutter UI
/// and the SKComm daemon.
///
/// **Send flow**
/// ```
/// ChatMessage
///   → ChatCrypto.signAndWrap  (RSA-signed MessageEnvelope)
///   → MessageEnvelope.toWireFormat  (JSON string)
///   → SKCommClient.sendMessage  (POST /api/v1/send)
///   → SKComm daemon → P2P transport → recipient
/// ```
///
/// **Receive flow**
/// ```
/// SKComm daemon (GET /api/v1/inbox)
///   → InboxMessage
///   → MessageEnvelope.tryParse  (JSON parse; graceful fallback to raw text)
///   → ChatCrypto.unwrap  (extract plaintext + metadata)
///   → ChatMessage  (stored in Hive, displayed in UI)
/// ```
class ChatTransportBridge {
  const ChatTransportBridge({
    required SKCommClient client,
    required IdentityService identity,
  })  : _client = client,
        _identity = identity;

  final SKCommClient _client;
  final IdentityService _identity;

  // ── Send ──────────────────────────────────────────────────────────────────

  /// Send [message] to its [ChatMessage.peerId] via the SKComm daemon.
  ///
  /// 1. Loads the local PGP keypair from secure storage.
  /// 2. Signs the content and wraps it in a [MessageEnvelope].
  /// 3. Serialises the envelope to JSON and POSTs to the daemon.
  ///
  /// Falls back to sending the raw plaintext when no local keypair is stored
  /// (e.g. during onboarding before key generation completes).
  ///
  /// Returns the daemon-assigned envelope ID on success, null on failure.
  Future<String?> sendMessage({
    required ChatMessage message,
    String? threadId,
    String? inReplyTo,
  }) async {
    try {
      final keyPair = await _identity.load();

      final String wirePayload;
      if (keyPair != null) {
        final envelope = await ChatCrypto.signAndWrap(
          id: message.id,
          sender: keyPair.fingerprint,
          content: message.content,
          privateKeyPem: keyPair.privateKeyPem,
          timestamp: message.timestamp,
          threadId: threadId,
          inReplyTo: inReplyTo,
        );
        wirePayload = envelope.toWireFormat();
      } else {
        // No local key — send raw content (unsigned).
        wirePayload = message.content;
      }

      final result = await _client.sendMessage(
        recipient: message.peerId,
        message: wirePayload,
        threadId: threadId,
        inReplyTo: inReplyTo,
      );
      return result.delivered ? result.envelopeId : null;
    } catch (_) {
      return null;
    }
  }

  // ── Receive ───────────────────────────────────────────────────────────────

  /// Convert an [InboxMessage] from the daemon into a [ChatMessage] for the UI.
  ///
  /// Attempts to parse the content as a [MessageEnvelope].  If parsing
  /// succeeds the plaintext [payload] and reply metadata are extracted;
  /// otherwise the raw content string is used as-is (backward compatibility
  /// with peers that do not yet use the envelope format).
  ChatMessage toIncoming(InboxMessage msg) {
    String content = msg.content;
    String? replyToId = msg.inReplyTo;

    final envelope = MessageEnvelope.tryParse(msg.content);
    if (envelope != null) {
      final unwrapped = ChatCrypto.unwrap(envelope);
      content = unwrapped.content;
      replyToId = unwrapped.replyToId ?? replyToId;
    }

    return ChatMessage(
      id: msg.envelopeId,
      peerId: msg.sender,
      content: content,
      timestamp: msg.createdAt,
      isOutbound: false,
      deliveryStatus: 'delivered',
      isEncrypted: msg.isEncrypted,
      replyToId: replyToId,
    );
  }
}

// ── Riverpod provider ──────────────────────────────────────────────────────

final chatTransportBridgeProvider = Provider<ChatTransportBridge>((ref) {
  return ChatTransportBridge(
    client: ref.watch(skcommClientProvider),
    identity: ref.watch(identityServiceProvider),
  );
});
