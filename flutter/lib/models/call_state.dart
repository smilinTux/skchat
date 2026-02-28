import 'package:flutter/material.dart';

enum CallType { voice, video }

enum CallStatus {
  idle,
  ringing,     // outgoing: dialing | incoming: alerting
  connecting,  // ICE negotiation
  active,      // media flowing
  ended,
  failed,
}

/// Real-time call quality metrics derived from WebRTC stats.
class CallQuality {
  const CallQuality({
    this.signalBars = 4,
    this.jitterMs,
    this.rttMs,
    this.packetLossPercent,
  });

  /// Signal strength 0–4 (4 = excellent, 0 = no signal).
  final int signalBars;
  final double? jitterMs;
  final double? rttMs;
  final int? packetLossPercent;

  factory CallQuality.fromStats(Map<String, dynamic> stats) {
    final jitter = (stats['jitterMs'] as num?)?.toDouble();
    final rtt = (stats['rttMs'] as num?)?.toDouble();
    final lost = stats['packetsLostPercent'] as int?;

    int bars = 4;
    if (rtt != null && rtt > 200) bars--;
    if (rtt != null && rtt > 500) bars--;
    if (jitter != null && jitter > 50) bars--;
    if (lost != null && lost > 5) bars--;

    return CallQuality(
      signalBars: bars.clamp(0, 4),
      jitterMs: jitter,
      rttMs: rtt,
      packetLossPercent: lost,
    );
  }
}

/// Immutable snapshot of an ongoing or pending call.
class CallState {
  const CallState({
    required this.status,
    required this.type,
    required this.peerId,
    required this.peerName,
    required this.peerSoulColor,
    this.isIncoming = false,
    this.isMuted = false,
    this.isCameraOff = false,
    this.isSpeakerOn = false,
    this.isPiP = false,
    this.quality = const CallQuality(),
    this.startedAt,
    this.errorMessage,
  });

  final CallStatus status;
  final CallType type;
  final String peerId;
  final String peerName;
  final Color peerSoulColor;

  /// True when we are the callee (peer called us).
  final bool isIncoming;

  /// Local audio muted.
  final bool isMuted;

  /// Local camera disabled (video call only).
  final bool isCameraOff;

  /// Loudspeaker routing active.
  final bool isSpeakerOn;

  /// Picture-in-picture mode active.
  final bool isPiP;

  final CallQuality quality;
  final DateTime? startedAt;
  final String? errorMessage;

  Duration get duration {
    if (startedAt == null) return Duration.zero;
    return DateTime.now().difference(startedAt!);
  }

  String get formattedDuration {
    final d = duration;
    final m = d.inMinutes.remainder(60).toString().padLeft(2, '0');
    final s = d.inSeconds.remainder(60).toString().padLeft(2, '0');
    if (d.inHours > 0) return '${d.inHours}:$m:$s';
    return '$m:$s';
  }

  CallState copyWith({
    CallStatus? status,
    CallType? type,
    bool? isIncoming,
    bool? isMuted,
    bool? isCameraOff,
    bool? isSpeakerOn,
    bool? isPiP,
    CallQuality? quality,
    DateTime? startedAt,
    String? errorMessage,
  }) {
    return CallState(
      status: status ?? this.status,
      type: type ?? this.type,
      peerId: peerId,
      peerName: peerName,
      peerSoulColor: peerSoulColor,
      isIncoming: isIncoming ?? this.isIncoming,
      isMuted: isMuted ?? this.isMuted,
      isCameraOff: isCameraOff ?? this.isCameraOff,
      isSpeakerOn: isSpeakerOn ?? this.isSpeakerOn,
      isPiP: isPiP ?? this.isPiP,
      quality: quality ?? this.quality,
      startedAt: startedAt ?? this.startedAt,
      errorMessage: errorMessage ?? this.errorMessage,
    );
  }
}
