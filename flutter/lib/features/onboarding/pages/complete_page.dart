import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../../core/theme/sovereign_colors.dart';
import '../onboarding_provider.dart';

/// Onboarding step 5 — celebration screen that marks onboarding complete and
/// navigates to the main chat list.
class CompletePage extends ConsumerStatefulWidget {
  const CompletePage({super.key, this.onEnterChat});

  /// Called after [OnboardingNotifier.markComplete] resolves.
  final VoidCallback? onEnterChat;

  @override
  ConsumerState<CompletePage> createState() => _CompletePageState();
}

class _CompletePageState extends ConsumerState<CompletePage>
    with SingleTickerProviderStateMixin {
  late AnimationController _confettiController;
  late Animation<double> _scaleIn;
  late Animation<double> _fadeIn;
  bool _entering = false;

  @override
  void initState() {
    super.initState();
    _confettiController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1200),
    )..forward();

    _scaleIn = CurvedAnimation(
      parent: _confettiController,
      curve: const Interval(0.0, 0.6, curve: Curves.elasticOut),
    );

    _fadeIn = CurvedAnimation(
      parent: _confettiController,
      curve: const Interval(0.3, 1.0, curve: Curves.easeOut),
    );
  }

  @override
  void dispose() {
    _confettiController.dispose();
    super.dispose();
  }

  Future<void> _enterChat() async {
    setState(() => _entering = true);
    await ref.read(onboardingProvider.notifier).markComplete();
    widget.onEnterChat?.call();
  }

  @override
  Widget build(BuildContext context) {
    final identityChoice =
        ref.watch(onboardingProvider).identityChoice;
    final name =
        identityChoice == 'import' ? 'sovereign' : 'new sovereign';

    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 32, vertical: 48),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const Spacer(),
            // Animated crown / celebration icon.
            ScaleTransition(
              scale: _scaleIn,
              child: Center(
                child: Stack(
                  alignment: Alignment.center,
                  children: [
                    // Glow halo.
                    Container(
                      width: 140,
                      height: 140,
                      decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        gradient: RadialGradient(
                          colors: [
                            SovereignColors.soulLumina.withValues(alpha: 0.35),
                            SovereignColors.surfaceBase.withValues(alpha: 0),
                          ],
                        ),
                      ),
                    ),
                    Container(
                      width: 100,
                      height: 100,
                      decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        color: SovereignColors.soulLumina.withValues(alpha: 0.12),
                        border: Border.all(
                          color:
                              SovereignColors.soulLumina.withValues(alpha: 0.4),
                          width: 2,
                        ),
                      ),
                      child: const Icon(
                        Icons.verified,
                        size: 52,
                        color: SovereignColors.soulLumina,
                      ),
                    ),
                  ],
                ),
              ),
            ),
            const SizedBox(height: 40),
            FadeTransition(
              opacity: _fadeIn,
              child: Column(
                children: [
                  Text(
                    'Welcome to the kingdom,\n$name!',
                    textAlign: TextAlign.center,
                    style: const TextStyle(
                      fontSize: 26,
                      fontWeight: FontWeight.w700,
                      color: SovereignColors.textPrimary,
                      height: 1.35,
                    ),
                  ),
                  const SizedBox(height: 16),
                  const Text(
                    'Your sovereign node is ready.\nAll messages are encrypted end-to-end.\nNo one else has your keys.',
                    textAlign: TextAlign.center,
                    style: TextStyle(
                      fontSize: 14,
                      color: SovereignColors.textSecondary,
                      height: 1.6,
                    ),
                  ),
                  const SizedBox(height: 8),
                  // Confetti placeholder — rows of colored dots.
                  _ConfettiDots(),
                ],
              ),
            ),
            const Spacer(),
            FadeTransition(
              opacity: _fadeIn,
              child: FilledButton(
                onPressed: _entering ? null : _enterChat,
                style: FilledButton.styleFrom(
                  backgroundColor: SovereignColors.soulLumina,
                  foregroundColor: Colors.black,
                  disabledBackgroundColor: SovereignColors.surfaceGlass,
                  padding: const EdgeInsets.symmetric(vertical: 16),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(12),
                  ),
                ),
                child: _entering
                    ? const SizedBox(
                        width: 20,
                        height: 20,
                        child: CircularProgressIndicator(
                          strokeWidth: 2,
                          color: Colors.black,
                        ),
                      )
                    : const Text(
                        'Enter SKChat',
                        style: TextStyle(
                          fontSize: 16,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
              ),
            ),
            const SizedBox(height: 32),
          ],
        ),
      ),
    );
  }
}

/// Simple confetti placeholder rendered as animated colored dots.
class _ConfettiDots extends StatefulWidget {
  @override
  State<_ConfettiDots> createState() => _ConfettiDotsState();
}

class _ConfettiDotsState extends State<_ConfettiDots>
    with SingleTickerProviderStateMixin {
  late AnimationController _controller;

  static const _colors = [
    SovereignColors.soulLumina,
    SovereignColors.soulJarvis,
    SovereignColors.soulChef,
    SovereignColors.accentEncrypt,
    SovereignColors.accentWarning,
  ];

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 2000),
    )..repeat();
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _controller,
      builder: (context, _) {
        return SizedBox(
          height: 32,
          child: Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: List.generate(10, (i) {
              final offset = ((_controller.value + i * 0.1) % 1.0);
              final opacity = (offset < 0.5 ? offset * 2 : (1 - offset) * 2)
                  .clamp(0.2, 1.0);
              return Padding(
                padding: const EdgeInsets.symmetric(horizontal: 4),
                child: Transform.translate(
                  offset: Offset(0, -8 * opacity),
                  child: Container(
                    width: 8,
                    height: 8,
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      color: _colors[i % _colors.length]
                          .withValues(alpha: opacity),
                    ),
                  ),
                ),
              );
            }),
          ),
        );
      },
    );
  }
}
