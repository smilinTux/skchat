// GENERATED CODE - DO NOT MODIFY BY HAND

part of 'conversation.dart';

// **************************************************************************
// JsonSerializableGenerator
// **************************************************************************

_$ConversationImpl _$$ConversationImplFromJson(Map<String, dynamic> json) =>
    _$ConversationImpl(
      id: json['id'] as String,
      participantId: json['participantId'] as String,
      participantName: json['participantName'] as String,
      participantFingerprint: json['participantFingerprint'] as String?,
      isAgent: json['isAgent'] as bool? ?? false,
      isGroup: json['isGroup'] as bool? ?? false,
      lastMessage: json['lastMessage'] as String?,
      lastMessageTime: json['lastMessageTime'] == null
          ? null
          : DateTime.parse(json['lastMessageTime'] as String),
      unreadCount: (json['unreadCount'] as num?)?.toInt() ?? 0,
      isEncrypted: json['isEncrypted'] as bool? ?? true,
      presenceStatus: $enumDecodeNullable(
              _$PresenceStatusEnumMap, json['presenceStatus']) ??
          PresenceStatus.offline,
      typingIndicator: json['typingIndicator'] as String?,
      cloud9Score: (json['cloud9Score'] as num?)?.toDouble(),
    );

Map<String, dynamic> _$$ConversationImplToJson(_$ConversationImpl instance) =>
    <String, dynamic>{
      'id': instance.id,
      'participantId': instance.participantId,
      'participantName': instance.participantName,
      'participantFingerprint': instance.participantFingerprint,
      'isAgent': instance.isAgent,
      'isGroup': instance.isGroup,
      'lastMessage': instance.lastMessage,
      'lastMessageTime': instance.lastMessageTime?.toIso8601String(),
      'unreadCount': instance.unreadCount,
      'isEncrypted': instance.isEncrypted,
      'presenceStatus': _$PresenceStatusEnumMap[instance.presenceStatus]!,
      'typingIndicator': instance.typingIndicator,
      'cloud9Score': instance.cloud9Score,
    };

const _$PresenceStatusEnumMap = {
  PresenceStatus.online: 'online',
  PresenceStatus.offline: 'offline',
  PresenceStatus.away: 'away',
};
