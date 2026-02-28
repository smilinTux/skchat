import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../../core/theme/sovereign_colors.dart';
import '../../../models/call_state.dart';
import '../call_provider.dart';

/// Row of in-call control buttons: mute, camera (video only), speaker, PiP, end.
/// All buttons use glass-surface styling with Sovereign Glass tokens.
class CallControls extends ConsumerWidget {
  const CallControls({
    super.key,
    this.orientation = Axis.horizontal,
  });

  final Axis orientation;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final call = ref.watch(callProvider);
    if (call == null) return const SizedBox.shrink();

    final notifier = ref.read(callProvider.notifier);
    final isVideo = call.type == CallType.video;

    final buttons = [
      _ControlButton(
        icon: call.isMuted ? Icons.mic_off_rounded : Icons.mic_rounded,
        label: call.isMuted ? 'Unmute' : 'Mute',
        active: call.isMuted,
        activeColor: SovereignColors.accentWarning,
        onTap: notifier.toggleMute,
      ),
      if (isVideo)
        _ControlButton(
          icon: call.isCameraOff
              ? Icons.videocam_off_rounded
              : Icons.videocam_rounded,
          label: call.isCameraOff ? 'Camera on' : 'Camera off',
          active: call.isCameraOff,
          activeColor: SovereignColors.accentWarning,
          onTap: notifier.toggleCamera,
        ),
      _ControlButton(
        icon: call.isSpeakerOn
            ? Icons.volume_up_rounded
            : Icons.volume_down_rounded,
        label: call.isSpeakerOn ? 'Earpiece' : 'Speaker',
        active: call.isSpeakerOn,
        activeColor: call.peerSoulColor,
        onTap: notifier.toggleSpeaker,
      ),
      if (isVideo)
        _ControlButton(
          icon: call.isPiP
              ? Icons.picture_in_picture_alt_rounded
              : Icons.picture_in_picture_rounded,
          label: 'PiP',
          active: call.isPiP,
          activeColor: call.peerSoulColor,
          onTap: notifier.togglePiP,
        ),
    ];

    return orientation == Axis.horizontal
        ? Row(
            mainAxisAlignment: MainAxisAlignment.spaceEvenly,
            children: [
              ...buttons,
              _EndCallButton(onTap: notifier.hangUp),
            ],
          )
        : Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              ...buttons,
              const SizedBox(height: 8),
              _EndCallButton(onTap: notifier.hangUp),
            ],
          );
  }
}

/// Circular glass button with icon + label below.
class _ControlButton extends StatelessWidget {
  const _ControlButton({
    required this.icon,
    required this.label,
    required this.onTap,
    this.active = false,
    this.activeColor,
  });

  final IconData icon;
  final String label;
  final VoidCallback onTap;
  final bool active;
  final Color? activeColor;

  @override
  Widget build(BuildContext context) {
    final accent = activeColor ?? SovereignColors.soulLumina;

    return GestureDetector(
      onTap: onTap,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          AnimatedContainer(
            duration: const Duration(milliseconds: 200),
            curve: Curves.easeOutCubic,
            width: 56,
            height: 56,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: active
                  ? accent.withValues(alpha: 0.2)
                  : SovereignColors.surfaceGlass,
              border: Border.all(
                color: active
                    ? accent.withValues(alpha: 0.6)
                    : SovereignColors.surfaceGlassBorder,
                width: 1.5,
              ),
            ),
            child: Icon(
              icon,
              color: active ? accent : SovereignColors.textPrimary,
              size: 24,
            ),
          ),
          const SizedBox(height: 6),
          Text(
            label,
            style: const TextStyle(
              color: SovereignColors.textSecondary,
              fontSize: 11,
              fontWeight: FontWeight.w400,
            ),
          ),
        ],
      ),
    );
  }
}

/// Large red end-call button.
class _EndCallButton extends StatelessWidget {
  const _EndCallButton({required this.onTap});

  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(
            width: 64,
            height: 64,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: SovereignColors.accentDanger,
              boxShadow: [
                BoxShadow(
                  color: SovereignColors.accentDanger.withValues(alpha: 0.4),
                  blurRadius: 16,
                  spreadRadius: 2,
                ),
              ],
            ),
            child: const Icon(
              Icons.call_end_rounded,
              color: Colors.white,
              size: 28,
            ),
          ),
          const SizedBox(height: 6),
          const Text(
            'End',
            style: TextStyle(
              color: SovereignColors.accentDanger,
              fontSize: 11,
              fontWeight: FontWeight.w600,
            ),
          ),
        ],
      ),
    );
  }
}
