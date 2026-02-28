import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'package:go_router/go_router.dart';
import '../../../core/router/app_router.dart';
import '../../../core/theme/sovereign_colors.dart';
import '../../../models/call_state.dart';
import '../call_provider.dart';

/// Floating picture-in-picture overlay shown when an active call enters PiP mode.
///
/// Draggable mini window pinned to the top-right corner. Tapping it returns to
/// the full in-call screen. Shown on top of all other content via an Overlay.
class PiPOverlay extends ConsumerStatefulWidget {
  const PiPOverlay({super.key, required this.child});

  final Widget child;

  @override
  ConsumerState<PiPOverlay> createState() => _PiPOverlayState();
}

class _PiPOverlayState extends ConsumerState<PiPOverlay> {
  OverlayEntry? _entry;
  RTCVideoRenderer? _renderer;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _syncOverlay();
  }

  @override
  void dispose() {
    _removeOverlay();
    _renderer?.dispose();
    super.dispose();
  }

  void _syncOverlay() {
    final call = ref.read(callProvider);
    final isPiP = call?.isPiP == true && call?.status == CallStatus.active;

    if (isPiP && _entry == null) {
      _showOverlay(call!);
    } else if (!isPiP && _entry != null) {
      _removeOverlay();
    }
  }

  void _showOverlay(CallState call) {
    _renderer = RTCVideoRenderer();
    _renderer!.initialize().then((_) {
      final service = ref.read(callProvider.notifier).webrtcService;
      if (service?.localStream != null) {
        _renderer!.srcObject = service!.localStream;
      }
    });

    _entry = OverlayEntry(
      builder: (_) => _PiPWindow(
        call: call,
        renderer: _renderer,
        onTap: () {
          ref.read(callProvider.notifier).togglePiP();
          context.push(AppRoutes.inCallPath(call.peerId));
        },
        onHangUp: () {
          ref.read(callProvider.notifier).hangUp();
          _removeOverlay();
        },
      ),
    );

    Overlay.of(context).insert(_entry!);
  }

  void _removeOverlay() {
    _entry?.remove();
    _entry = null;
  }

  @override
  Widget build(BuildContext context) {
    ref.listen<CallState?>(callProvider, (prev, next) {
      final isPiP = next?.isPiP == true && next?.status == CallStatus.active;
      if (isPiP && _entry == null && next != null) {
        _showOverlay(next);
      } else if (!isPiP && _entry != null) {
        _removeOverlay();
      }
    });

    return widget.child;
  }
}

/// The draggable mini window content.
class _PiPWindow extends StatefulWidget {
  const _PiPWindow({
    required this.call,
    required this.renderer,
    required this.onTap,
    required this.onHangUp,
  });

  final CallState call;
  final RTCVideoRenderer? renderer;
  final VoidCallback onTap;
  final VoidCallback onHangUp;

  @override
  State<_PiPWindow> createState() => _PiPWindowState();
}

class _PiPWindowState extends State<_PiPWindow> {
  double _right = 16;
  double _top = 80;

  @override
  Widget build(BuildContext context) {
    return Positioned(
      right: _right,
      top: _top,
      child: GestureDetector(
        onPanUpdate: (d) {
          setState(() {
            _right = (_right - d.delta.dx).clamp(0.0, 300.0);
            _top = (_top + d.delta.dy).clamp(
              MediaQuery.of(context).padding.top,
              MediaQuery.of(context).size.height - 140,
            );
          });
        },
        onTap: widget.onTap,
        child: Material(
          color: Colors.transparent,
          child: ClipRRect(
            borderRadius: BorderRadius.circular(16),
            child: Container(
              width: 100,
              height: 130,
              decoration: BoxDecoration(
                color: SovereignColors.surfaceRaised,
                borderRadius: BorderRadius.circular(16),
                border: Border.all(
                  color: widget.call.peerSoulColor.withValues(alpha: 0.5),
                  width: 1.5,
                ),
                boxShadow: [
                  BoxShadow(
                    color: Colors.black.withValues(alpha: 0.5),
                    blurRadius: 16,
                    offset: const Offset(0, 4),
                  ),
                ],
              ),
              child: Stack(
                children: [
                  // Video preview (or avatar for voice).
                  if (widget.renderer != null &&
                      widget.call.type == CallType.video)
                    RTCVideoView(
                      widget.renderer!,
                      objectFit:
                          RTCVideoViewObjectFit.RTCVideoViewObjectFitCover,
                    )
                  else
                    _VoicePiPContent(call: widget.call),

                  // Duration overlay.
                  Positioned(
                    bottom: 28,
                    left: 0,
                    right: 0,
                    child: Text(
                      widget.call.formattedDuration,
                      textAlign: TextAlign.center,
                      style: const TextStyle(
                        color: Colors.white,
                        fontSize: 11,
                        fontWeight: FontWeight.w600,
                        shadows: [
                          Shadow(color: Colors.black54, blurRadius: 4),
                        ],
                      ),
                    ),
                  ),

                  // Hang-up button.
                  Positioned(
                    bottom: 6,
                    left: 0,
                    right: 0,
                    child: GestureDetector(
                      onTap: widget.onHangUp,
                      child: const Center(
                        child: Icon(
                          Icons.call_end_rounded,
                          color: SovereignColors.accentDanger,
                          size: 20,
                        ),
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _VoicePiPContent extends StatelessWidget {
  const _VoicePiPContent({required this.call});

  final CallState call;

  @override
  Widget build(BuildContext context) {
    return Container(
      color: call.peerSoulColor.withValues(alpha: 0.12),
      child: Center(
        child: Text(
          call.peerName.isNotEmpty ? call.peerName[0].toUpperCase() : '?',
          style: TextStyle(
            color: call.peerSoulColor,
            fontSize: 28,
            fontWeight: FontWeight.w700,
          ),
        ),
      ),
    );
  }
}
