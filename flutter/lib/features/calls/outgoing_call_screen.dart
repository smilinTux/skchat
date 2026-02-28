import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import '../../core/router/app_router.dart';
import '../../core/theme/sovereign_colors.dart';
import '../../core/theme/glass_widgets.dart';
import '../../models/call_state.dart';
import 'call_provider.dart';
import 'widgets/call_quality_indicator.dart';

/// Full-screen outgoing call screen shown after tapping the call button.
///
/// Displays the peer's soul-color avatar with a slow pulse and a "Calling…"
/// indicator. Transitions automatically to InCallScreen when the connection
/// becomes active or pops back if the call ends/fails.
class OutgoingCallScreen extends ConsumerStatefulWidget {
  const OutgoingCallScreen({super.key, required this.peerId});

  final String peerId;

  @override
  ConsumerState<OutgoingCallScreen> createState() => _OutgoingCallScreenState();
}

class _OutgoingCallScreenState extends ConsumerState<OutgoingCallScreen>
    with SingleTickerProviderStateMixin {
  late AnimationController _pulseCtrl;
  late Animation<double> _pulseAnim;

  @override
  void initState() {
    super.initState();
    _pulseCtrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 2000),
    )..repeat(reverse: true);

    _pulseAnim = Tween<double>(begin: 1.0, end: 1.12).animate(
      CurvedAnimation(parent: _pulseCtrl, curve: Curves.easeInOut),
    );
  }

  @override
  void dispose() {
    _pulseCtrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    ref.listen<CallState?>(callProvider, (prev, next) {
      if (!mounted) return;
      if (next == null) {
        // Call ended / failed — return to conversation.
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
    final isConnecting = call.status == CallStatus.connecting;

    return Scaffold(
      backgroundColor: SovereignColors.surfaceBase,
      body: Stack(
        children: [
          // Soul-color radial glow background.
          Positioned.fill(
            child: DecoratedBox(
              decoration: BoxDecoration(
                gradient: RadialGradient(
                  center: const Alignment(0, -0.2),
                  radius: 0.8,
                  colors: [
                    soul.withValues(alpha: 0.14),
                    SovereignColors.surfaceBase,
                  ],
                ),
              ),
            ),
          ),

          SafeArea(
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 32),
              child: Column(
                children: [
                  const SizedBox(height: 60),

                  // Status line.
                  Text(
                    isConnecting ? 'Connecting…' : 'Calling…',
                    style: TextStyle(
                      color: soul.withValues(alpha: 0.8),
                      fontSize: 14,
                      fontWeight: FontWeight.w500,
                      letterSpacing: 0.6,
                    ),
                  ),

                  const SizedBox(height: 40),

                  // Pulsing avatar.
                  AnimatedBuilder(
                    animation: _pulseAnim,
                    builder: (_, __) => Transform.scale(
                      scale: _pulseAnim.value,
                      child: _CallAvatar(
                        peerName: call.peerName,
                        soulColor: soul,
                        size: 120,
                      ),
                    ),
                  ),

                  const SizedBox(height: 28),

                  // Peer name.
                  Text(
                    call.peerName,
                    style: const TextStyle(
                      color: SovereignColors.textPrimary,
                      fontSize: 28,
                      fontWeight: FontWeight.w700,
                    ),
                  ),

                  const SizedBox(height: 8),

                  // Call type label.
                  Text(
                    call.type == CallType.video ? 'Video call' : 'Voice call',
                    style: const TextStyle(
                      color: SovereignColors.textSecondary,
                      fontSize: 14,
                    ),
                  ),

                  if (isConnecting) ...[
                    const SizedBox(height: 16),
                    CallQualityIndicator(
                      quality: call.quality,
                      size: 14,
                      showLabel: true,
                    ),
                  ],

                  const Spacer(),

                  // Cancel button.
                  GestureDetector(
                    onTap: () {
                      ref.read(callProvider.notifier).hangUp();
                      if (context.canPop()) context.pop();
                    },
                    child: Container(
                      width: 72,
                      height: 72,
                      decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        color: SovereignColors.accentDanger,
                        boxShadow: [
                          BoxShadow(
                            color:
                                SovereignColors.accentDanger.withValues(alpha: 0.4),
                            blurRadius: 20,
                            spreadRadius: 2,
                          ),
                        ],
                      ),
                      child: const Icon(
                        Icons.call_end_rounded,
                        color: Colors.white,
                        size: 30,
                      ),
                    ),
                  ),

                  const SizedBox(height: 8),

                  const Text(
                    'Cancel',
                    style: TextStyle(
                      color: SovereignColors.accentDanger,
                      fontSize: 13,
                    ),
                  ),

                  const SizedBox(height: 56),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// Shared soul-color circular avatar used across call screens.
class _CallAvatar extends StatelessWidget {
  const _CallAvatar({
    required this.peerName,
    required this.soulColor,
    this.size = 100,
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
        border: Border.all(
          color: soulColor.withValues(alpha: 0.6),
          width: 2.5,
        ),
        boxShadow: [
          BoxShadow(
            color: soulColor.withValues(alpha: 0.25),
            blurRadius: 32,
            spreadRadius: 4,
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
