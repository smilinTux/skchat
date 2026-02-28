import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import '../../core/theme/sovereign_colors.dart';
import '../../models/call_state.dart';
import '../../services/skcomm_client.dart';
import '../../services/webrtc_service.dart';

/// Riverpod notifier managing the full lifecycle of a voice/video call.
///
/// State is null when no call is active/pending.
/// State transitions: null → ringing → connecting → active → null
class CallNotifier extends Notifier<CallState?> {
  WebRTCCallService? _webrtc;
  StreamSubscription<RTCPeerConnectionState>? _connSub;
  StreamSubscription<Map<String, dynamic>>? _statsSub;
  Timer? _durationTimer;

  @override
  CallState? build() => null;

  // ── Outgoing call ─────────────────────────────────────────────────────────

  Future<void> initiateCall({
    required String peerId,
    required String peerName,
    required Color peerSoulColor,
    required CallType type,
  }) async {
    if (state != null) return; // already in a call

    state = CallState(
      status: CallStatus.ringing,
      type: type,
      peerId: peerId,
      peerName: peerName,
      peerSoulColor: peerSoulColor,
      isIncoming: false,
    );

    try {
      final client = ref.read(skcommClientProvider);

      // Notify peer via SKComm message with sentinel prefix.
      await client.sendMessage(
        recipient: peerId,
        message: '__CALL_REQUEST__:${type.name}',
      );

      final iceServers = await client.getIceConfig();

      _webrtc = WebRTCCallService(signalingBaseUrl: 'ws://localhost:9384');
      await _webrtc!.initLocalMedia(withVideo: type == CallType.video);
      await _webrtc!.connect(
        roomId: _roomId(peerId),
        iceServers: iceServers,
        isOfferer: true,
      );

      state = state?.copyWith(status: CallStatus.connecting);
      _subscribeToWebRTC();
    } catch (e) {
      state = state?.copyWith(
        status: CallStatus.failed,
        errorMessage: e.toString(),
      );
    }
  }

  // ── Incoming call ─────────────────────────────────────────────────────────

  /// Presents an incoming call notification.
  /// Called from the sync layer when a __CALL_REQUEST__ message arrives.
  void incomingCall({
    required String peerId,
    required String peerName,
    required Color peerSoulColor,
    CallType type = CallType.voice,
  }) {
    // Don't interrupt an active call with a new incoming.
    if (state?.status == CallStatus.active) return;
    state = CallState(
      status: CallStatus.ringing,
      type: type,
      peerId: peerId,
      peerName: peerName,
      peerSoulColor: peerSoulColor,
      isIncoming: true,
    );
  }

  Future<void> acceptCall() async {
    final current = state;
    if (current == null || !current.isIncoming) return;
    state = current.copyWith(status: CallStatus.connecting);

    try {
      final iceServers = await ref.read(skcommClientProvider).getIceConfig();

      _webrtc = WebRTCCallService(signalingBaseUrl: 'ws://localhost:9384');
      await _webrtc!.initLocalMedia(withVideo: current.type == CallType.video);
      await _webrtc!.connect(
        roomId: _roomId(current.peerId),
        iceServers: iceServers,
        isOfferer: false,
      );
      _subscribeToWebRTC();
    } catch (e) {
      state = state?.copyWith(
        status: CallStatus.failed,
        errorMessage: e.toString(),
      );
    }
  }

  void rejectCall() {
    _cleanup();
    state = null;
  }

  // ── Active call controls ──────────────────────────────────────────────────

  void hangUp() {
    _cleanup();
    state = null;
  }

  void toggleMute() {
    final current = state;
    if (current == null) return;
    final muted = !current.isMuted;
    _webrtc?.setMuted(muted);
    state = current.copyWith(isMuted: muted);
  }

  void toggleCamera() {
    final current = state;
    if (current == null) return;
    final off = !current.isCameraOff;
    _webrtc?.setCameraEnabled(!off);
    state = current.copyWith(isCameraOff: off);
  }

  void toggleSpeaker() {
    final current = state;
    if (current == null) return;
    state = current.copyWith(isSpeakerOn: !current.isSpeakerOn);
  }

  void togglePiP() {
    final current = state;
    if (current == null) return;
    state = current.copyWith(isPiP: !current.isPiP);
  }

  // ── Internals ─────────────────────────────────────────────────────────────

  WebRTCCallService? get webrtcService => _webrtc;

  void _subscribeToWebRTC() {
    _connSub = _webrtc?.connectionState.listen((s) {
      switch (s) {
        case RTCPeerConnectionState.RTCPeerConnectionStateConnected:
          state = state?.copyWith(
            status: CallStatus.active,
            startedAt: DateTime.now(),
          );
          _startDurationTick();
        case RTCPeerConnectionState.RTCPeerConnectionStateFailed:
        case RTCPeerConnectionState.RTCPeerConnectionStateDisconnected:
        case RTCPeerConnectionState.RTCPeerConnectionStateClosed:
          _cleanup();
          state = null;
        default:
          break;
      }
    });

    _statsSub = _webrtc?.statsStream.listen((stats) {
      state = state?.copyWith(quality: CallQuality.fromStats(stats));
    });
  }

  void _startDurationTick() {
    _durationTimer?.cancel();
    _durationTimer = Timer.periodic(const Duration(seconds: 1), (_) {
      if (state?.status == CallStatus.active) {
        // Poke a new identical state so the UI rebuilds the duration counter.
        state = state?.copyWith();
      }
    });
  }

  void _cleanup() {
    _durationTimer?.cancel();
    _connSub?.cancel();
    _statsSub?.cancel();
    _webrtc?.dispose();
    _webrtc = null;
    _durationTimer = null;
    _connSub = null;
    _statsSub = null;
  }

  String _roomId(String peerId) => 'call-$peerId';
}

final callProvider = NotifierProvider<CallNotifier, CallState?>(
  CallNotifier.new,
);

/// Convenience provider — true when a call is active (for PiP overlay).
final hasActiveCallProvider = Provider<bool>((ref) {
  final call = ref.watch(callProvider);
  return call != null && call.status == CallStatus.active;
});
