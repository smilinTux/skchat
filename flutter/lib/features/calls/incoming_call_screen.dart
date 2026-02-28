import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import '../../core/router/app_router.dart';
import '../../core/theme/sovereign_colors.dart';
import '../../models/call_state.dart';
import 'call_provider.dart';

/// Full-screen incoming call alert with Sovereign Glass styling.
///
/// Shows the caller's soul-color avatar with a ring-pulse animation.
/// Accept (green) and Decline (red) buttons at the bottom.
class IncomingCallScreen extends ConsumerStatefulWidget {
  const IncomingCallScreen({super.key, required this.peerId});

  final String peerId;

  @override
  ConsumerState<IncomingCallScreen> createState() => _IncomingCallScreenState();
}

class _IncomingCallScreenState extends ConsumerState<IncomingCallScreen>
    with SingleTickerProviderStateMixin {
  late AnimationController _ringCtrl;
  late Animation<double> _ringScale;
  late Animation<double> _ringOpacity;

  @override
  void initState() {
    super.initState();
    _ringCtrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1400),
    )..repeat();

    _ringScale = Tween<double>(begin: 1.0, end: 1.6).animate(
      CurvedAnimation(parent: _ringCtrl, curve: Curves.easeOut),
    );
    _ringOpacity = Tween<double>(begin: 0.5, end: 0.0).animate(
      CurvedAnimation(parent: _ringCtrl, curve: Curves.easeOut),
    );
  }

  @override
  void dispose() {
    _ringCtrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    ref.listen<CallState?>(callProvider, (prev, next) {
      if (!mounted) return;
      if (next == null) {
        if (context.canPop()) context.pop();
        return;
      }
      if (next.status == CallStatus.active) {
        context.pushReplacement(AppRoutes.inCallPath(next.peerId));
      }
    });

    final call = ref.watch(callProvider);
    if (call == null) return const SizedBox.shrink();

    final soul = call.peerSoulColor;

    return Scaffold(
      backgroundColor: SovereignColors.surfaceBase,
      body: Stack(
        children: [
          // Radial glow background.
          Positioned.fill(
            child: DecoratedBox(
              decoration: BoxDecoration(
                gradient: RadialGradient(
                  center: const Alignment(0, -0.2),
                  radius: 0.85,
                  colors: [
                    soul.withValues(alpha: 0.18),
                    SovereignColors.surfaceBase,
                  ],
                ),
              ),
            ),
          ),

          SafeArea(
            child: Column(
              children: [
                const SizedBox(height: 70),

                // "Incoming call" label.
                Text(
                  call.type == CallType.video
                      ? 'Incoming video call'
                      : 'Incoming voice call',
                  style: TextStyle(
                    color: soul.withValues(alpha: 0.85),
                    fontSize: 14,
                    fontWeight: FontWeight.w500,
                    letterSpacing: 0.6,
                  ),
                ),

                const SizedBox(height: 44),

                // Ripple ring + avatar.
                SizedBox(
                  width: 160,
                  height: 160,
                  child: Stack(
                    alignment: Alignment.center,
                    children: [
                      // Animated ripple ring.
                      AnimatedBuilder(
                        animation: _ringCtrl,
                        builder: (_, __) => Transform.scale(
                          scale: _ringScale.value,
                          child: Container(
                            width: 120,
                            height: 120,
                            decoration: BoxDecoration(
                              shape: BoxShape.circle,
                              border: Border.all(
                                color: soul.withValues(
                                    alpha: _ringOpacity.value),
                                width: 2,
                              ),
                            ),
                          ),
                        ),
                      ),
                      // Avatar.
                      _IncomingAvatar(
                        peerName: call.peerName,
                        soulColor: soul,
                      ),
                    ],
                  ),
                ),

                const SizedBox(height: 28),

                // Caller name.
                Text(
                  call.peerName,
                  style: const TextStyle(
                    color: SovereignColors.textPrimary,
                    fontSize: 28,
                    fontWeight: FontWeight.w700,
                  ),
                ),

                const SizedBox(height: 8),

                // Peer ID (fingerprint prefix for verification).
                Text(
                  call.peerId.length > 16
                      ? call.peerId.substring(0, 16).toUpperCase()
                      : call.peerId.toUpperCase(),
                  style: const TextStyle(
                    color: SovereignColors.textTertiary,
                    fontSize: 12,
                    fontFamily: 'monospace',
                    letterSpacing: 1.2,
                  ),
                ),

                const Spacer(),

                // Accept / Decline row.
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 60),
                  child: Row(
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: [
                      // Decline.
                      _CallActionButton(
                        icon: Icons.call_end_rounded,
                        label: 'Decline',
                        color: SovereignColors.accentDanger,
                        onTap: () {
                          ref.read(callProvider.notifier).rejectCall();
                          if (context.canPop()) context.pop();
                        },
                      ),

                      // Accept.
                      _CallActionButton(
                        icon: call.type == CallType.video
                            ? Icons.videocam_rounded
                            : Icons.call_rounded,
                        label: 'Accept',
                        color: SovereignColors.accentEncrypt,
                        onTap: () => ref.read(callProvider.notifier).acceptCall(),
                      ),
                    ],
                  ),
                ),

                const SizedBox(height: 64),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _IncomingAvatar extends StatelessWidget {
  const _IncomingAvatar({
    required this.peerName,
    required this.soulColor,
  });

  final String peerName;
  final Color soulColor;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 100,
      height: 100,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        color: soulColor.withValues(alpha: 0.15),
        border: Border.all(color: soulColor.withValues(alpha: 0.7), width: 2.5),
        boxShadow: [
          BoxShadow(
            color: soulColor.withValues(alpha: 0.3),
            blurRadius: 24,
            spreadRadius: 4,
          ),
        ],
      ),
      child: Center(
        child: Text(
          peerName.isNotEmpty ? peerName[0].toUpperCase() : '?',
          style: TextStyle(
            color: soulColor,
            fontSize: 36,
            fontWeight: FontWeight.w700,
          ),
        ),
      ),
    );
  }
}

class _CallActionButton extends StatelessWidget {
  const _CallActionButton({
    required this.icon,
    required this.label,
    required this.color,
    required this.onTap,
  });

  final IconData icon;
  final String label;
  final Color color;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(
            width: 72,
            height: 72,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: color,
              boxShadow: [
                BoxShadow(
                  color: color.withValues(alpha: 0.4),
                  blurRadius: 20,
                  spreadRadius: 2,
                ),
              ],
            ),
            child: Icon(icon, color: Colors.white, size: 30),
          ),
          const SizedBox(height: 8),
          Text(
            label,
            style: TextStyle(
              color: color,
              fontSize: 13,
              fontWeight: FontWeight.w600,
            ),
          ),
        ],
      ),
    );
  }
}
