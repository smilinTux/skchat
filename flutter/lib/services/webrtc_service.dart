import 'dart:async';
import 'dart:convert';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

/// Manages a single WebRTC peer connection for voice/video calls.
///
/// Connects to the SKComm signaling broker WebSocket for SDP/ICE exchange.
/// [signalingBaseUrl] should be 'ws://localhost:9384' for the local daemon.
///
/// Platform note: add microphone + camera permissions in:
///   Android: AndroidManifest.xml (<uses-permission android:name="android.permission.CAMERA" />)
///   iOS: Info.plist (NSMicrophoneUsageDescription, NSCameraUsageDescription)
class WebRTCCallService {
  WebRTCCallService({required this.signalingBaseUrl});

  final String signalingBaseUrl;

  RTCPeerConnection? _pc;
  MediaStream? _localStream;
  WebSocketChannel? _signaling;
  Timer? _statsTimer;

  final _remoteStreamCtl = StreamController<MediaStream?>.broadcast();
  final _connStateCtl =
      StreamController<RTCPeerConnectionState>.broadcast();
  final _statsCtl = StreamController<Map<String, dynamic>>.broadcast();

  Stream<MediaStream?> get remoteStream => _remoteStreamCtl.stream;
  Stream<RTCPeerConnectionState> get connectionState => _connStateCtl.stream;
  Stream<Map<String, dynamic>> get statsStream => _statsCtl.stream;

  MediaStream? get localStream => _localStream;

  // ── Media ─────────────────────────────────────────────────────────────────

  /// Acquires the local camera/microphone stream.
  /// Call before [connect]. For voice-only calls pass withVideo=false.
  Future<MediaStream> initLocalMedia({bool withVideo = false}) async {
    _localStream = await navigator.mediaDevices.getUserMedia({
      'audio': true,
      'video': withVideo
          ? {'facingMode': 'user', 'width': 640, 'height': 480}
          : false,
    });
    return _localStream!;
  }

  // ── Connection ────────────────────────────────────────────────────────────

  /// Connects to the signaling broker and sets up the peer connection.
  ///
  /// [iceServers] — list from GET /api/v1/webrtc/ice-config.
  /// [roomId]    — signaling room, e.g. 'call-<callerFp>-<calleeFp>'.
  /// [isOfferer] — true for the caller; false for the callee.
  Future<void> connect({
    required String roomId,
    required List<Map<String, dynamic>> iceServers,
    required bool isOfferer,
  }) async {
    await _createPeerConnection(iceServers);
    await _connectSignaling(roomId);
    if (isOfferer) await _makeOffer();
  }

  Future<void> _createPeerConnection(
      List<Map<String, dynamic>> iceServers) async {
    final config = <String, dynamic>{
      'iceServers': iceServers,
      'sdpSemantics': 'unified-plan',
    };

    _pc = await createPeerConnection(config);

    // Add local tracks before negotiation.
    if (_localStream != null) {
      for (final track in _localStream!.getTracks()) {
        _pc!.addTrack(track, _localStream!);
      }
    }

    _pc!.onTrack = (RTCTrackEvent event) {
      if (event.streams.isNotEmpty) {
        _remoteStreamCtl.add(event.streams[0]);
      }
    };

    _pc!.onConnectionState = (RTCPeerConnectionState s) =>
        _connStateCtl.add(s);

    _pc!.onIceCandidate = (RTCIceCandidate c) {
      if (c.candidate == null || c.candidate!.isEmpty) return;
      _signalingEmit({
        'type': 'ice',
        'candidate': {
          'candidate': c.candidate,
          'sdpMid': c.sdpMid,
          'sdpMLineIndex': c.sdpMLineIndex,
        },
      });
    };

    _statsTimer =
        Timer.periodic(const Duration(seconds: 3), (_) => _pollStats());
  }

  Future<void> _connectSignaling(String roomId) async {
    final uri = Uri.parse('$signalingBaseUrl/webrtc/ws?room=$roomId');
    _signaling = WebSocketChannel.connect(uri);
    _signaling!.stream.listen(
      (data) => _handleSignalingMessage(data as String),
      onError: (_) {},
      onDone: () {},
      cancelOnError: false,
    );
  }

  void _signalingEmit(Map<String, dynamic> msg) {
    _signaling?.sink.add(jsonEncode(msg));
  }

  Future<void> _handleSignalingMessage(String raw) async {
    try {
      final msg = jsonDecode(raw) as Map<String, dynamic>;
      switch (msg['type'] as String?) {
        case 'offer':
          await _handleOffer(msg);
        case 'answer':
          await _handleAnswer(msg);
        case 'ice':
          await _handleIce(msg);
      }
    } catch (_) {}
  }

  Future<void> _makeOffer() async {
    final offer = await _pc!.createOffer({
      'offerToReceiveAudio': 1,
      'offerToReceiveVideo': 1,
    });
    await _pc!.setLocalDescription(offer);
    _signalingEmit({'type': 'offer', 'sdp': offer.toMap()});
  }

  Future<void> _handleOffer(Map<String, dynamic> msg) async {
    final sdp = msg['sdp'] as Map<String, dynamic>;
    await _pc!.setRemoteDescription(
      RTCSessionDescription(sdp['sdp'] as String, sdp['type'] as String),
    );
    final answer = await _pc!.createAnswer();
    await _pc!.setLocalDescription(answer);
    _signalingEmit({'type': 'answer', 'sdp': answer.toMap()});
  }

  Future<void> _handleAnswer(Map<String, dynamic> msg) async {
    final sdp = msg['sdp'] as Map<String, dynamic>;
    await _pc!.setRemoteDescription(
      RTCSessionDescription(sdp['sdp'] as String, sdp['type'] as String),
    );
  }

  Future<void> _handleIce(Map<String, dynamic> msg) async {
    final c = msg['candidate'] as Map<String, dynamic>;
    await _pc?.addCandidate(RTCIceCandidate(
      c['candidate'] as String,
      c['sdpMid'] as String?,
      c['sdpMLineIndex'] as int?,
    ));
  }

  // ── Stats ─────────────────────────────────────────────────────────────────

  Future<void> _pollStats() async {
    if (_pc == null) return;
    try {
      final reports = await _pc!.getStats();
      double? jitterMs;
      double? rttMs;
      int? lostPercent;

      for (final r in reports) {
        if (r.type == 'remote-inbound-rtp') {
          final jitter = r.values['jitter'] as num?;
          if (jitter != null) jitterMs = jitter.toDouble() * 1000;

          final rtt = r.values['roundTripTime'] as num?;
          if (rtt != null) rttMs = rtt.toDouble() * 1000;

          final lost = r.values['packetsLost'] as num?;
          final recv = r.values['packetsReceived'] as num?;
          if (lost != null && recv != null && recv > 0) {
            lostPercent = ((lost / recv) * 100).round();
          }
        }
      }

      _statsCtl.add({
        'jitterMs': jitterMs,
        'rttMs': rttMs,
        'packetsLostPercent': lostPercent,
      });
    } catch (_) {}
  }

  // ── Controls ──────────────────────────────────────────────────────────────

  void setMuted(bool muted) {
    for (final t in _localStream?.getAudioTracks() ?? []) {
      t.enabled = !muted;
    }
  }

  void setCameraEnabled(bool enabled) {
    for (final t in _localStream?.getVideoTracks() ?? []) {
      t.enabled = enabled;
    }
  }

  // ── Cleanup ───────────────────────────────────────────────────────────────

  Future<void> dispose() async {
    _statsTimer?.cancel();
    await _signaling?.sink.close();
    for (final t in _localStream?.getTracks() ?? []) {
      await t.stop();
    }
    await _localStream?.dispose();
    await _pc?.close();
    await _pc?.dispose();
    if (!_remoteStreamCtl.isClosed) await _remoteStreamCtl.close();
    if (!_connStateCtl.isClosed) await _connStateCtl.close();
    if (!_statsCtl.isClosed) await _statsCtl.close();
  }
}
