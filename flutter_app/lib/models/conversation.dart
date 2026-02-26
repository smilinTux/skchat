import 'package:freezed_annotation/freezed_annotation.dart';

part 'conversation.freezed.dart';
part 'conversation.g.dart';

/// Conversation/thread model
@freezed
class Conversation with _$Conversation {
  const factory Conversation({
    required String id,
    required String participantId,
    required String participantName,
    String? participantFingerprint,
    @Default(false) bool isAgent,
    @Default(false) bool isGroup,
    String? lastMessage,
    DateTime? lastMessageTime,
    @Default(0) int unreadCount,
    @Default(true) bool isEncrypted,
    @Default(PresenceStatus.offline) PresenceStatus presenceStatus,
    String? typingIndicator,
    double? cloud9Score,
  }) = _Conversation;

  factory Conversation.fromJson(Map<String, dynamic> json) =>
      _$ConversationFromJson(json);
}

/// Presence status for participants
enum PresenceStatus {
  online,
  offline,
  away,
}
