/// ChatMessage mirrors the skchat Python ChatMessage model.
class ChatMessage {
  const ChatMessage({
    required this.id,
    required this.peerId,
    required this.content,
    required this.timestamp,
    required this.isOutbound,
    this.deliveryStatus = 'sent',
    this.isEncrypted = true,
    this.replyToId,
    this.reactions = const {},
    this.isAgent = false,
    this.senderName,
  });

  final String id;
  final String peerId;
  final String content;
  final DateTime timestamp;
  final bool isOutbound;

  /// 'sent' | 'delivered' | 'read'
  final String deliveryStatus;
  final bool isEncrypted;
  final String? replyToId;

  /// emoji → count
  final Map<String, int> reactions;
  final bool isAgent;
  final String? senderName;

  ChatMessage copyWith({
    String? id,
    String? peerId,
    String? content,
    DateTime? timestamp,
    bool? isOutbound,
    String? deliveryStatus,
    bool? isEncrypted,
    String? replyToId,
    Map<String, int>? reactions,
    bool? isAgent,
    String? senderName,
  }) {
    return ChatMessage(
      id: id ?? this.id,
      peerId: peerId ?? this.peerId,
      content: content ?? this.content,
      timestamp: timestamp ?? this.timestamp,
      isOutbound: isOutbound ?? this.isOutbound,
      deliveryStatus: deliveryStatus ?? this.deliveryStatus,
      isEncrypted: isEncrypted ?? this.isEncrypted,
      replyToId: replyToId ?? this.replyToId,
      reactions: reactions ?? this.reactions,
      isAgent: isAgent ?? this.isAgent,
      senderName: senderName ?? this.senderName,
    );
  }

  factory ChatMessage.fromJson(Map<String, dynamic> json) {
    return ChatMessage(
      id: json['id'] as String? ?? '',
      peerId: json['peer_id'] as String? ?? '',
      content: json['content'] as String? ?? '',
      timestamp: json['timestamp'] != null
          ? DateTime.parse(json['timestamp'] as String)
          : DateTime.now(),
      isOutbound: json['is_outbound'] as bool? ?? false,
      deliveryStatus: json['delivery_status'] as String? ?? 'sent',
      isEncrypted: json['is_encrypted'] as bool? ?? true,
      replyToId: json['reply_to_id'] as String?,
      reactions: (json['reactions'] as Map<String, dynamic>?)?.map(
            (k, v) => MapEntry(k, v as int),
          ) ??
          const {},
      isAgent: json['is_agent'] as bool? ?? false,
      senderName: json['sender_name'] as String?,
    );
  }

  Map<String, dynamic> toJson() => {
    'id': id,
    'peer_id': peerId,
    'content': content,
    'timestamp': timestamp.toIso8601String(),
    'is_outbound': isOutbound,
    'delivery_status': deliveryStatus,
    'is_encrypted': isEncrypted,
    'reply_to_id': replyToId,
    'reactions': reactions,
    'is_agent': isAgent,
    'sender_name': senderName,
  };
}
