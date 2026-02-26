import 'package:hive_flutter/hive_flutter.dart';
import '../models/chat_message.dart';
import '../models/conversation.dart';

/// Hive type IDs — keep unique across the app.
const int chatMessageTypeId = 0;
const int conversationTypeId = 1;

class ChatMessageAdapter extends TypeAdapter<ChatMessage> {
  @override
  final int typeId = chatMessageTypeId;

  @override
  ChatMessage read(BinaryReader reader) {
    final map = reader.readMap().cast<String, dynamic>();
    return ChatMessage(
      id: map['id'] as String? ?? '',
      peerId: map['peer_id'] as String? ?? '',
      content: map['content'] as String? ?? '',
      timestamp: DateTime.fromMillisecondsSinceEpoch(
        map['timestamp'] as int? ?? 0,
      ),
      isOutbound: map['is_outbound'] as bool? ?? false,
      deliveryStatus: map['delivery_status'] as String? ?? 'sent',
      isEncrypted: map['is_encrypted'] as bool? ?? true,
      replyToId: map['reply_to_id'] as String?,
      reactions: (map['reactions'] as Map?)?.cast<String, int>() ?? const {},
      isAgent: map['is_agent'] as bool? ?? false,
      senderName: map['sender_name'] as String?,
    );
  }

  @override
  void write(BinaryWriter writer, ChatMessage obj) {
    writer.writeMap(<String, dynamic>{
      'id': obj.id,
      'peer_id': obj.peerId,
      'content': obj.content,
      'timestamp': obj.timestamp.millisecondsSinceEpoch,
      'is_outbound': obj.isOutbound,
      'delivery_status': obj.deliveryStatus,
      'is_encrypted': obj.isEncrypted,
      'reply_to_id': obj.replyToId,
      'reactions': obj.reactions,
      'is_agent': obj.isAgent,
      'sender_name': obj.senderName,
    });
  }
}

class ConversationAdapter extends TypeAdapter<Conversation> {
  @override
  final int typeId = conversationTypeId;

  @override
  Conversation read(BinaryReader reader) {
    final map = reader.readMap().cast<String, dynamic>();
    return Conversation(
      peerId: map['peer_id'] as String? ?? '',
      displayName: map['display_name'] as String? ?? '',
      lastMessage: map['last_message'] as String? ?? '',
      lastMessageTime: DateTime.fromMillisecondsSinceEpoch(
        map['last_message_time'] as int? ?? 0,
      ),
      soulFingerprint: map['soul_fingerprint'] as String?,
      isOnline: map['is_online'] as bool? ?? false,
      isAgent: map['is_agent'] as bool? ?? false,
      unreadCount: map['unread_count'] as int? ?? 0,
      lastDeliveryStatus: map['last_delivery_status'] as String? ?? 'sent',
      isTyping: false,
      isGroup: map['is_group'] as bool? ?? false,
      memberCount: map['member_count'] as int? ?? 0,
      avatarUrl: map['avatar_url'] as String?,
    );
  }

  @override
  void write(BinaryWriter writer, Conversation obj) {
    writer.writeMap(<String, dynamic>{
      'peer_id': obj.peerId,
      'display_name': obj.displayName,
      'last_message': obj.lastMessage,
      'last_message_time': obj.lastMessageTime.millisecondsSinceEpoch,
      'soul_fingerprint': obj.soulFingerprint,
      'is_online': obj.isOnline,
      'is_agent': obj.isAgent,
      'unread_count': obj.unreadCount,
      'last_delivery_status': obj.lastDeliveryStatus,
      'is_group': obj.isGroup,
      'member_count': obj.memberCount,
      'avatar_url': obj.avatarUrl,
    });
  }
}
