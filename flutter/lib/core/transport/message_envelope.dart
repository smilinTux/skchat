import 'dart:convert';

/// Wire-format wrapper for a chat message sent over the SKComm transport layer.
///
/// Serialised as a JSON string so the SKComm daemon can forward it opaquely
/// without needing to understand the application payload.
///
/// Receivers that pre-date this format will see the raw JSON string as the
/// message body — [tryParse] returns null in that case so callers fall back
/// to treating the raw string as plaintext.
class MessageEnvelope {
  const MessageEnvelope({
    required this.id,
    required this.sender,
    required this.payload,
    required this.timestamp,
    this.signature,
    this.threadId,
    this.inReplyTo,
  });

  /// Matches the ChatMessage.id used for the optimistic local insert.
  final String id;

  /// Sender's PGP fingerprint or peer name.
  final String sender;

  /// Plaintext message content (after any transport-layer decryption by daemon).
  final String payload;

  final DateTime timestamp;

  /// Optional base64 RSA-PKCS1v15-SHA256 signature over [payload] bytes.
  /// Present only when the sender had a local keypair loaded at send time.
  final String? signature;

  final String? threadId;
  final String? inReplyTo;

  Map<String, dynamic> toJson() => {
    'skchat_envelope': true,
    'id': id,
    'sender': sender,
    'payload': payload,
    'timestamp': timestamp.toIso8601String(),
    if (signature != null) 'signature': signature,
    if (threadId != null) 'thread_id': threadId,
    if (inReplyTo != null) 'in_reply_to': inReplyTo,
  };

  /// JSON string suitable for passing as the `message` field in
  /// [SKCommClient.sendMessage].
  String toWireFormat() => jsonEncode(toJson());

  factory MessageEnvelope.fromJson(Map<String, dynamic> json) {
    return MessageEnvelope(
      id: json['id'] as String? ?? '',
      sender: json['sender'] as String? ?? '',
      payload: json['payload'] as String? ?? '',
      timestamp: json['timestamp'] != null
          ? DateTime.parse(json['timestamp'] as String)
          : DateTime.now(),
      signature: json['signature'] as String?,
      threadId: json['thread_id'] as String?,
      inReplyTo: json['in_reply_to'] as String?,
    );
  }

  /// Attempt to parse [content] as a [MessageEnvelope].
  ///
  /// Returns null if [content] is not valid JSON or does not carry the
  /// `skchat_envelope` marker — callers should treat it as raw plaintext.
  static MessageEnvelope? tryParse(String content) {
    try {
      final decoded = jsonDecode(content);
      if (decoded is! Map<String, dynamic>) return null;
      if (decoded['skchat_envelope'] != true) return null;
      if (!decoded.containsKey('payload')) return null;
      return MessageEnvelope.fromJson(decoded);
    } catch (_) {
      return null;
    }
  }
}
