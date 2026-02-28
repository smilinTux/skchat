import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'package:go_router/go_router.dart';
import '../../core/theme/sovereign_colors.dart';
import '../../models/call_state.dart';
import 'call_provider.dart';
import 'widgets/call_controls.dart';
import 'widgets/call_quality_indicator.dart';

/// Full-screen active call screen.
///
/// Voice call: large soul-color avatar + controls.
/// Video call: full-screen remote video + draggable local PiP tile.
///
/// Navigates back to the previous chat screen when the call ends.
class InCallScreen extends ConsumerStatefulWidget {
  const InCallScreen({super.key, required this.peerId});

  final String peerId;

  @override
  ConsumerState<InCallScreen> createState() => _InCallScreenState();
}

class _InCallScreenState extends ConsumerState<InCallScreen>
    with SingleTickerProviderStateMixin {
  final RTCVideoRenderer _remoteRenderer = RTCVideoRenderer();
  final RTCVideoRenderer _localRenderer = RTCVideoRenderer();

  late AnimationController _voicePulseCtrl;
  late Animation<double> _voicePulse;
  StreamSubscription<MediaStream?>? _remoteSub;

  bool _controlsVisible = true;
  Timer? _controlsHideTimer;

  @override
  void initState() {
    super.initState();

    _voicePulseCtrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1600),
    )..repeat(reverse: true);

    _voicePulse = Tween<double>(begin: 1.0, end: 1.08).animate(
      CurvedAnimation(parent: _voicePulseCtrl, curve: Curves.easeInOut),
    );

    _initRenderers();
    _scheduleControlsHide();
  }

  Future<void> _initRenderers() async {
    await _remoteRenderer.initialize();
    await _localRenderer.initialize();

    final service = ref.read(callProvider.notifier).webrtcService;
    if (service != null) {
      // Attach local stream immediately.
      if (service.localStream != null) {
        _localRenderer.srcObject = service.localStream;
      }

      // Subscribe for when the remote stream arrives.
      _remoteSub = service.remoteStream.listen((stream) {
        if (mounted) {
          setState(() {
            _remoteRenderer.srcObject = stream;
          });
        }
      });
    }
  }

  void _scheduleControlsHide() {
    _controlsHideTimer?.cancel();
    _controlsHideTimer = Timer(const Duration(seconds: 4), () {
      if (mounted) setState(() => _controlsVisible = false);
    });
  }

  void _onTapScreen() {
    setState(() => _controlsVisible = true);
    _scheduleControlsHide();
  }

  @override
  void dispose() {
    _controlsHideTimer?.cancel();
    _voicePulseCtrl.dispose();
    _remoteSub?.cancel();
    _remoteRenderer.dispose();
    _localRenderer.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    ref.listen<CallState?>(callProvider, (prev, next) {
      if (!mounted) return;
      if (next == null || next.status == CallStatus.ended ||
          next.status == CallStatus.failed) {
        // Pop all call screens back to the chat.
        while (context.canPop()) {
          context.pop();
        }
      }
    });

    final call = ref.watch(callProvider);
    if (call == null) return const SizedBox.shrink();

    final isVideo = call.type == CallType.video;

    return Scaffold(
      backgroundColor: SovereignColors.surfaceBase,
      body: GestureDetector(
        onTap: _onTapScreen,
        child: Stack(
          children: [
            // ── Main content ──────────────────────────────────────────────

            if (isVideo)
              _VideoCallLayout(
                call: call,
                remoteRenderer: _remoteRenderer,
                localRenderer: _localRenderer,
              )
            else
              _VoiceCallLayout(
                call: call,
                pulseAnimation: _voicePulse,
              ),

            // ── Top bar ───────────────────────────────────────────────────

            AnimatedOpacity(
              opacity: _controlsVisible ? 1.0 : 0.0,
              duration: const Duration(milliseconds: 300),
              child: SafeArea(
                child: Padding(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 20, vertical: 8),
                  child: Row(
                    children: [
                      // Back button navigates back but keeps call alive (for PiP).
                      IconButton(
                        icon: const Icon(
                          Icons.keyboard_arrow_down_rounded,
                          color: SovereignColors.textPrimary,
                          size: 28,
                        ),
                        onPressed: () {
                          if (isVideo) {
                            ref.read(callProvider.notifier).togglePiP();
                          }
                          if (context.canPop()) context.pop();
                        },
                        tooltip: isVideo ? 'PiP mode' : 'Minimise',
                      ),

                      const Spacer(),

                      // Duration + quality.
                      Column(
                        mainAxisSize: MainAxisSize.min,
                        crossAxisAlignment: CrossAxisAlignment.end,
                        children: [
                          Text(
                            call.formattedDuration,
                            style: const TextStyle(
                              color: SovereignColors.textPrimary,
                              fontSize: 14,
                              fontWeight: FontWeight.w600,
                              fontFamily: 'monospace',
                            ),
                          ),
                          const SizedBox(height: 2),
                          CallQualityIndicator(quality: call.quality, size: 12),
                        ],
                      ),
                    ],
                  ),
                ),
              ),
            ),

            // ── Control bar ───────────────────────────────────────────────

            AnimatedPositioned(
              duration: const Duration(milliseconds: 300),
              curve: Curves.easeOutCubic,
              bottom: _controlsVisible ? 0 : -160,
              left: 0,
              right: 0,
              child: _ControlsBar(child: const CallControls()),
            ),
          ],
        ),
      ),
    );
  }
}

// ── Layout helpers ──────────────────────────────────────────────────────────

/// Voice call: large avatar centred, optional mute indicator.
class _VoiceCallLayout extends StatelessWidget {
  const _VoiceCallLayout({
    required this.call,
    required this.pulseAnimation,
  });

  final CallState call;
  final Animation<double> pulseAnimation;

  @override
  Widget build(BuildContext context) {
    final soul = call.peerSoulColor;

    return SizedBox.expand(
      child: DecoratedBox(
        decoration: BoxDecoration(
          gradient: RadialGradient(
            center: const Alignment(0, -0.1),
            radius: 0.9,
            colors: [
              soul.withValues(alpha: 0.12),
              SovereignColors.surfaceBase,
            ],
          ),
        ),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            // Pulsing avatar for voice activity.
            AnimatedBuilder(
              animation: pulseAnimation,
              builder: (_, __) => Transform.scale(
                scale: call.isMuted ? 1.0 : pulseAnimation.value,
                child: _LargeCallAvatar(
                  peerName: call.peerName,
                  soulColor: soul,
                  size: 140,
                ),
              ),
            ),

            const SizedBox(height: 28),

            Text(
              call.peerName,
              style: const TextStyle(
                color: SovereignColors.textPrimary,
                fontSize: 26,
                fontWeight: FontWeight.w700,
              ),
            ),

            const SizedBox(height: 8),

            if (call.isMuted)
              Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(
                    Icons.mic_off_rounded,
                    size: 14,
                    color: SovereignColors.accentWarning.withValues(alpha: 0.8),
                  ),
                  const SizedBox(width: 4),
                  Text(
                    'Muted',
                    style: TextStyle(
                      color:
                          SovereignColors.accentWarning.withValues(alpha: 0.8),
                      fontSize: 13,
                    ),
                  ),
                ],
              )
            else
              const Text(
                'Connected',
                style: TextStyle(
                  color: SovereignColors.accentEncrypt,
                  fontSize: 13,
                ),
              ),

            // Extra space so controls don't overlap avatar.
            const SizedBox(height: 140),
          ],
        ),
      ),
    );
  }
}

/// Video call: full-screen remote video with local thumbnail in corner.
class _VideoCallLayout extends ConsumerStatefulWidget {
  const _VideoCallLayout({
    required this.call,
    required this.remoteRenderer,
    required this.localRenderer,
  });

  final CallState call;
  final RTCVideoRenderer remoteRenderer;
  final RTCVideoRenderer localRenderer;

  @override
  ConsumerState<_VideoCallLayout> createState() => _VideoCallLayoutState();
}

class _VideoCallLayoutState extends ConsumerState<_VideoCallLayout> {
  double _localRight = 16;
  double _localBottom = 160; // above the controls bar

  @override
  Widget build(BuildContext context) {
    final size = MediaQuery.of(context).size;

    return Stack(
      children: [
        // Remote video — full screen.
        Positioned.fill(
          child: RTCVideoView(
            widget.remoteRenderer,
            objectFit: RTCVideoViewObjectFit.RTCVideoViewObjectFitCover,
          ),
        ),

        // Local video thumbnail — draggable.
        Positioned(
          right: _localRight,
          bottom: _localBottom,
          child: GestureDetector(
            onPanUpdate: (d) {
              setState(() {
                _localRight =
                    (_localRight - d.delta.dx).clamp(0.0, size.width - 90.0);
                _localBottom =
                    (_localBottom - d.delta.dy).clamp(160.0, size.height - 160.0);
              });
            },
            child: ClipRRect(
              borderRadius: BorderRadius.circular(12),
              child: SizedBox(
                width: 88,
                height: 120,
                child: widget.call.isCameraOff
                    ? ColoredBox(
                        color: SovereignColors.surfaceRaised,
                        child: Icon(
                          Icons.videocam_off_rounded,
                          color: SovereignColors.textTertiary,
                          size: 28,
                        ),
                      )
                    : RTCVideoView(
                        widget.localRenderer,
                        mirror: true,
                        objectFit:
                            RTCVideoViewObjectFit.RTCVideoViewObjectFitCover,
                      ),
              ),
            ),
          ),
        ),

        // Peer name overlay (top of remote video).
        Positioned(
          top: MediaQuery.of(context).padding.top + 56,
          left: 20,
          child: Text(
            widget.call.peerName,
            style: const TextStyle(
              color: Colors.white,
              fontSize: 16,
              fontWeight: FontWeight.w600,
              shadows: [Shadow(color: Colors.black54, blurRadius: 6)],
            ),
          ),
        ),
      ],
    );
  }
}

/// Large circular avatar for voice calls.
class _LargeCallAvatar extends StatelessWidget {
  const _LargeCallAvatar({
    required this.peerName,
    required this.soulColor,
    this.size = 120,
  });

  final String peerName;
  final Color soulColor;
  final double size;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: size,
      height: size,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        color: soulColor.withValues(alpha: 0.15),
        border: Border.all(color: soulColor.withValues(alpha: 0.7), width: 3),
        boxShadow: [
          BoxShadow(
            color: soulColor.withValues(alpha: 0.3),
            blurRadius: 40,
            spreadRadius: 6,
          ),
        ],
      ),
      child: Center(
        child: Text(
          peerName.isNotEmpty ? peerName[0].toUpperCase() : '?',
          style: TextStyle(
            color: soulColor,
            fontSize: size * 0.36,
            fontWeight: FontWeight.w700,
          ),
        ),
      ),
    );
  }
}

/// Frosted glass panel at the bottom holding the call controls.
class _ControlsBar extends StatelessWidget {
  const _ControlsBar({required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) {
    final bottom = MediaQuery.of(context).padding.bottom;
    return Container(
      padding: EdgeInsets.fromLTRB(20, 20, 20, 20 + bottom),
      decoration: BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: [
            Colors.transparent,
            Colors.black.withValues(alpha: 0.7),
          ],
        ),
      ),
      child: child,
    );
  }
}
