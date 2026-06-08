// GENERATED CODE - DO NOT MODIFY BY HAND

part of 'chat_message.dart';

// **************************************************************************
// JsonSerializableGenerator
// **************************************************************************

_$ChatMessageImpl _$$ChatMessageImplFromJson(Map<String, dynamic> json) =>
    _$ChatMessageImpl(
      id: json['id'] as String,
      conversationId: json['conversationId'] as String,
      senderId: json['senderId'] as String,
      senderName: json['senderName'] as String,
      content: json['content'] as String,
      timestamp: DateTime.parse(json['timestamp'] as String),
      isEncrypted: json['isEncrypted'] as bool? ?? false,
      status: $enumDecodeNullable(_$MessageStatusEnumMap, json['status']) ??
          MessageStatus.sending,
      replyToId: json['replyToId'] as String?,
      reactions: (json['reactions'] as List<dynamic>?)
          ?.map((e) => Reaction.fromJson(e as Map<String, dynamic>))
          .toList(),
      ttl: (json['ttl'] as num?)?.toInt(),
      attachments: (json['attachments'] as List<dynamic>?)
          ?.map((e) => e as String)
          .toList(),
    );

Map<String, dynamic> _$$ChatMessageImplToJson(_$ChatMessageImpl instance) =>
    <String, dynamic>{
      'id': instance.id,
      'conversationId': instance.conversationId,
      'senderId': instance.senderId,
      'senderName': instance.senderName,
      'content': instance.content,
      'timestamp': instance.timestamp.toIso8601String(),
      'isEncrypted': instance.isEncrypted,
      'status': _$MessageStatusEnumMap[instance.status]!,
      'replyToId': instance.replyToId,
      'reactions': instance.reactions,
      'ttl': instance.ttl,
      'attachments': instance.attachments,
    };

const _$MessageStatusEnumMap = {
  MessageStatus.sending: 'sending',
  MessageStatus.sent: 'sent',
  MessageStatus.delivered: 'delivered',
  MessageStatus.read: 'read',
  MessageStatus.failed: 'failed',
};

_$ReactionImpl _$$ReactionImplFromJson(Map<String, dynamic> json) =>
    _$ReactionImpl(
      emoji: json['emoji'] as String,
      userId: json['userId'] as String,
      userName: json['userName'] as String,
      timestamp: DateTime.parse(json['timestamp'] as String),
    );

Map<String, dynamic> _$$ReactionImplToJson(_$ReactionImpl instance) =>
    <String, dynamic>{
      'emoji': instance.emoji,
      'userId': instance.userId,
      'userName': instance.userName,
      'timestamp': instance.timestamp.toIso8601String(),
    };
