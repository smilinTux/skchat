// coverage:ignore-file
// GENERATED CODE - DO NOT MODIFY BY HAND
// ignore_for_file: type=lint
// ignore_for_file: unused_element, deprecated_member_use, deprecated_member_use_from_same_package, use_function_type_syntax_for_parameters, unnecessary_const, avoid_init_to_null, invalid_override_different_default_values_named, prefer_expression_function_bodies, annotate_overrides, invalid_annotation_target, unnecessary_question_mark

part of 'conversation.dart';

// **************************************************************************
// FreezedGenerator
// **************************************************************************

T _$identity<T>(T value) => value;

final _privateConstructorUsedError = UnsupportedError(
    'It seems like you constructed your class using `MyClass._()`. This constructor is only meant to be used by freezed and you are not supposed to need it nor use it.\nPlease check the documentation here for more information: https://github.com/rrousselGit/freezed#adding-getters-and-methods-to-our-models');

Conversation _$ConversationFromJson(Map<String, dynamic> json) {
  return _Conversation.fromJson(json);
}

/// @nodoc
mixin _$Conversation {
  String get id => throw _privateConstructorUsedError;
  String get participantId => throw _privateConstructorUsedError;
  String get participantName => throw _privateConstructorUsedError;
  String? get participantFingerprint => throw _privateConstructorUsedError;
  bool get isAgent => throw _privateConstructorUsedError;
  bool get isGroup => throw _privateConstructorUsedError;
  String? get lastMessage => throw _privateConstructorUsedError;
  DateTime? get lastMessageTime => throw _privateConstructorUsedError;
  int get unreadCount => throw _privateConstructorUsedError;
  bool get isEncrypted => throw _privateConstructorUsedError;
  PresenceStatus get presenceStatus => throw _privateConstructorUsedError;
  String? get typingIndicator => throw _privateConstructorUsedError;
  double? get cloud9Score => throw _privateConstructorUsedError;

  /// Serializes this Conversation to a JSON map.
  Map<String, dynamic> toJson() => throw _privateConstructorUsedError;

  /// Create a copy of Conversation
  /// with the given fields replaced by the non-null parameter values.
  @JsonKey(includeFromJson: false, includeToJson: false)
  $ConversationCopyWith<Conversation> get copyWith =>
      throw _privateConstructorUsedError;
}

/// @nodoc
abstract class $ConversationCopyWith<$Res> {
  factory $ConversationCopyWith(
          Conversation value, $Res Function(Conversation) then) =
      _$ConversationCopyWithImpl<$Res, Conversation>;
  @useResult
  $Res call(
      {String id,
      String participantId,
      String participantName,
      String? participantFingerprint,
      bool isAgent,
      bool isGroup,
      String? lastMessage,
      DateTime? lastMessageTime,
      int unreadCount,
      bool isEncrypted,
      PresenceStatus presenceStatus,
      String? typingIndicator,
      double? cloud9Score});
}

/// @nodoc
class _$ConversationCopyWithImpl<$Res, $Val extends Conversation>
    implements $ConversationCopyWith<$Res> {
  _$ConversationCopyWithImpl(this._value, this._then);

  // ignore: unused_field
  final $Val _value;
  // ignore: unused_field
  final $Res Function($Val) _then;

  /// Create a copy of Conversation
  /// with the given fields replaced by the non-null parameter values.
  @pragma('vm:prefer-inline')
  @override
  $Res call({
    Object? id = null,
    Object? participantId = null,
    Object? participantName = null,
    Object? participantFingerprint = freezed,
    Object? isAgent = null,
    Object? isGroup = null,
    Object? lastMessage = freezed,
    Object? lastMessageTime = freezed,
    Object? unreadCount = null,
    Object? isEncrypted = null,
    Object? presenceStatus = null,
    Object? typingIndicator = freezed,
    Object? cloud9Score = freezed,
  }) {
    return _then(_value.copyWith(
      id: null == id
          ? _value.id
          : id // ignore: cast_nullable_to_non_nullable
              as String,
      participantId: null == participantId
          ? _value.participantId
          : participantId // ignore: cast_nullable_to_non_nullable
              as String,
      participantName: null == participantName
          ? _value.participantName
          : participantName // ignore: cast_nullable_to_non_nullable
              as String,
      participantFingerprint: freezed == participantFingerprint
          ? _value.participantFingerprint
          : participantFingerprint // ignore: cast_nullable_to_non_nullable
              as String?,
      isAgent: null == isAgent
          ? _value.isAgent
          : isAgent // ignore: cast_nullable_to_non_nullable
              as bool,
      isGroup: null == isGroup
          ? _value.isGroup
          : isGroup // ignore: cast_nullable_to_non_nullable
              as bool,
      lastMessage: freezed == lastMessage
          ? _value.lastMessage
          : lastMessage // ignore: cast_nullable_to_non_nullable
              as String?,
      lastMessageTime: freezed == lastMessageTime
          ? _value.lastMessageTime
          : lastMessageTime // ignore: cast_nullable_to_non_nullable
              as DateTime?,
      unreadCount: null == unreadCount
          ? _value.unreadCount
          : unreadCount // ignore: cast_nullable_to_non_nullable
              as int,
      isEncrypted: null == isEncrypted
          ? _value.isEncrypted
          : isEncrypted // ignore: cast_nullable_to_non_nullable
              as bool,
      presenceStatus: null == presenceStatus
          ? _value.presenceStatus
          : presenceStatus // ignore: cast_nullable_to_non_nullable
              as PresenceStatus,
      typingIndicator: freezed == typingIndicator
          ? _value.typingIndicator
          : typingIndicator // ignore: cast_nullable_to_non_nullable
              as String?,
      cloud9Score: freezed == cloud9Score
          ? _value.cloud9Score
          : cloud9Score // ignore: cast_nullable_to_non_nullable
              as double?,
    ) as $Val);
  }
}

/// @nodoc
abstract class _$$ConversationImplCopyWith<$Res>
    implements $ConversationCopyWith<$Res> {
  factory _$$ConversationImplCopyWith(
          _$ConversationImpl value, $Res Function(_$ConversationImpl) then) =
      __$$ConversationImplCopyWithImpl<$Res>;
  @override
  @useResult
  $Res call(
      {String id,
      String participantId,
      String participantName,
      String? participantFingerprint,
      bool isAgent,
      bool isGroup,
      String? lastMessage,
      DateTime? lastMessageTime,
      int unreadCount,
      bool isEncrypted,
      PresenceStatus presenceStatus,
      String? typingIndicator,
      double? cloud9Score});
}

/// @nodoc
class __$$ConversationImplCopyWithImpl<$Res>
    extends _$ConversationCopyWithImpl<$Res, _$ConversationImpl>
    implements _$$ConversationImplCopyWith<$Res> {
  __$$ConversationImplCopyWithImpl(
      _$ConversationImpl _value, $Res Function(_$ConversationImpl) _then)
      : super(_value, _then);

  /// Create a copy of Conversation
  /// with the given fields replaced by the non-null parameter values.
  @pragma('vm:prefer-inline')
  @override
  $Res call({
    Object? id = null,
    Object? participantId = null,
    Object? participantName = null,
    Object? participantFingerprint = freezed,
    Object? isAgent = null,
    Object? isGroup = null,
    Object? lastMessage = freezed,
    Object? lastMessageTime = freezed,
    Object? unreadCount = null,
    Object? isEncrypted = null,
    Object? presenceStatus = null,
    Object? typingIndicator = freezed,
    Object? cloud9Score = freezed,
  }) {
    return _then(_$ConversationImpl(
      id: null == id
          ? _value.id
          : id // ignore: cast_nullable_to_non_nullable
              as String,
      participantId: null == participantId
          ? _value.participantId
          : participantId // ignore: cast_nullable_to_non_nullable
              as String,
      participantName: null == participantName
          ? _value.participantName
          : participantName // ignore: cast_nullable_to_non_nullable
              as String,
      participantFingerprint: freezed == participantFingerprint
          ? _value.participantFingerprint
          : participantFingerprint // ignore: cast_nullable_to_non_nullable
              as String?,
      isAgent: null == isAgent
          ? _value.isAgent
          : isAgent // ignore: cast_nullable_to_non_nullable
              as bool,
      isGroup: null == isGroup
          ? _value.isGroup
          : isGroup // ignore: cast_nullable_to_non_nullable
              as bool,
      lastMessage: freezed == lastMessage
          ? _value.lastMessage
          : lastMessage // ignore: cast_nullable_to_non_nullable
              as String?,
      lastMessageTime: freezed == lastMessageTime
          ? _value.lastMessageTime
          : lastMessageTime // ignore: cast_nullable_to_non_nullable
              as DateTime?,
      unreadCount: null == unreadCount
          ? _value.unreadCount
          : unreadCount // ignore: cast_nullable_to_non_nullable
              as int,
      isEncrypted: null == isEncrypted
          ? _value.isEncrypted
          : isEncrypted // ignore: cast_nullable_to_non_nullable
              as bool,
      presenceStatus: null == presenceStatus
          ? _value.presenceStatus
          : presenceStatus // ignore: cast_nullable_to_non_nullable
              as PresenceStatus,
      typingIndicator: freezed == typingIndicator
          ? _value.typingIndicator
          : typingIndicator // ignore: cast_nullable_to_non_nullable
              as String?,
      cloud9Score: freezed == cloud9Score
          ? _value.cloud9Score
          : cloud9Score // ignore: cast_nullable_to_non_nullable
              as double?,
    ));
  }
}

/// @nodoc
@JsonSerializable()
class _$ConversationImpl implements _Conversation {
  const _$ConversationImpl(
      {required this.id,
      required this.participantId,
      required this.participantName,
      this.participantFingerprint,
      this.isAgent = false,
      this.isGroup = false,
      this.lastMessage,
      this.lastMessageTime,
      this.unreadCount = 0,
      this.isEncrypted = true,
      this.presenceStatus = PresenceStatus.offline,
      this.typingIndicator,
      this.cloud9Score});

  factory _$ConversationImpl.fromJson(Map<String, dynamic> json) =>
      _$$ConversationImplFromJson(json);

  @override
  final String id;
  @override
  final String participantId;
  @override
  final String participantName;
  @override
  final String? participantFingerprint;
  @override
  @JsonKey()
  final bool isAgent;
  @override
  @JsonKey()
  final bool isGroup;
  @override
  final String? lastMessage;
  @override
  final DateTime? lastMessageTime;
  @override
  @JsonKey()
  final int unreadCount;
  @override
  @JsonKey()
  final bool isEncrypted;
  @override
  @JsonKey()
  final PresenceStatus presenceStatus;
  @override
  final String? typingIndicator;
  @override
  final double? cloud9Score;

  @override
  String toString() {
    return 'Conversation(id: $id, participantId: $participantId, participantName: $participantName, participantFingerprint: $participantFingerprint, isAgent: $isAgent, isGroup: $isGroup, lastMessage: $lastMessage, lastMessageTime: $lastMessageTime, unreadCount: $unreadCount, isEncrypted: $isEncrypted, presenceStatus: $presenceStatus, typingIndicator: $typingIndicator, cloud9Score: $cloud9Score)';
  }

  @override
  bool operator ==(Object other) {
    return identical(this, other) ||
        (other.runtimeType == runtimeType &&
            other is _$ConversationImpl &&
            (identical(other.id, id) || other.id == id) &&
            (identical(other.participantId, participantId) ||
                other.participantId == participantId) &&
            (identical(other.participantName, participantName) ||
                other.participantName == participantName) &&
            (identical(other.participantFingerprint, participantFingerprint) ||
                other.participantFingerprint == participantFingerprint) &&
            (identical(other.isAgent, isAgent) || other.isAgent == isAgent) &&
            (identical(other.isGroup, isGroup) || other.isGroup == isGroup) &&
            (identical(other.lastMessage, lastMessage) ||
                other.lastMessage == lastMessage) &&
            (identical(other.lastMessageTime, lastMessageTime) ||
                other.lastMessageTime == lastMessageTime) &&
            (identical(other.unreadCount, unreadCount) ||
                other.unreadCount == unreadCount) &&
            (identical(other.isEncrypted, isEncrypted) ||
                other.isEncrypted == isEncrypted) &&
            (identical(other.presenceStatus, presenceStatus) ||
                other.presenceStatus == presenceStatus) &&
            (identical(other.typingIndicator, typingIndicator) ||
                other.typingIndicator == typingIndicator) &&
            (identical(other.cloud9Score, cloud9Score) ||
                other.cloud9Score == cloud9Score));
  }

  @JsonKey(includeFromJson: false, includeToJson: false)
  @override
  int get hashCode => Object.hash(
      runtimeType,
      id,
      participantId,
      participantName,
      participantFingerprint,
      isAgent,
      isGroup,
      lastMessage,
      lastMessageTime,
      unreadCount,
      isEncrypted,
      presenceStatus,
      typingIndicator,
      cloud9Score);

  /// Create a copy of Conversation
  /// with the given fields replaced by the non-null parameter values.
  @JsonKey(includeFromJson: false, includeToJson: false)
  @override
  @pragma('vm:prefer-inline')
  _$$ConversationImplCopyWith<_$ConversationImpl> get copyWith =>
      __$$ConversationImplCopyWithImpl<_$ConversationImpl>(this, _$identity);

  @override
  Map<String, dynamic> toJson() {
    return _$$ConversationImplToJson(
      this,
    );
  }
}

abstract class _Conversation implements Conversation {
  const factory _Conversation(
      {required final String id,
      required final String participantId,
      required final String participantName,
      final String? participantFingerprint,
      final bool isAgent,
      final bool isGroup,
      final String? lastMessage,
      final DateTime? lastMessageTime,
      final int unreadCount,
      final bool isEncrypted,
      final PresenceStatus presenceStatus,
      final String? typingIndicator,
      final double? cloud9Score}) = _$ConversationImpl;

  factory _Conversation.fromJson(Map<String, dynamic> json) =
      _$ConversationImpl.fromJson;

  @override
  String get id;
  @override
  String get participantId;
  @override
  String get participantName;
  @override
  String? get participantFingerprint;
  @override
  bool get isAgent;
  @override
  bool get isGroup;
  @override
  String? get lastMessage;
  @override
  DateTime? get lastMessageTime;
  @override
  int get unreadCount;
  @override
  bool get isEncrypted;
  @override
  PresenceStatus get presenceStatus;
  @override
  String? get typingIndicator;
  @override
  double? get cloud9Score;

  /// Create a copy of Conversation
  /// with the given fields replaced by the non-null parameter values.
  @override
  @JsonKey(includeFromJson: false, includeToJson: false)
  _$$ConversationImplCopyWith<_$ConversationImpl> get copyWith =>
      throw _privateConstructorUsedError;
}
