import 'package:freezed_annotation/freezed_annotation.dart';

part 'chat_message.freezed.dart';
part 'chat_message.g.dart';

/// Message model mirroring skchat Python ChatMessage
@freezed
class ChatMessage with _$ChatMessage {
  const factory ChatMessage({
    required String id,
    required String conversationId,
    required String senderId,
    required String senderName,
    required String content,
    required DateTime timestamp,
    @Default(false) bool isEncrypted,
    @Default(MessageStatus.sending) MessageStatus status,
    String? replyToId,
    List<Reaction>? reactions,
    int? ttl,
    List<String>? attachments,
  }) = _ChatMessage;

  factory ChatMessage.fromJson(Map<String, dynamic> json) =>
      _$ChatMessageFromJson(json);
}

/// Message delivery status
enum MessageStatus {
  sending,
  sent,
  delivered,
  read,
  failed,
}

/// Message reaction
@freezed
class Reaction with _$Reaction {
  const factory Reaction({
    required String emoji,
    required String userId,
    required String userName,
    required DateTime timestamp,
  }) = _Reaction;

  factory Reaction.fromJson(Map<String, dynamic> json) =>
      _$ReactionFromJson(json);
}
