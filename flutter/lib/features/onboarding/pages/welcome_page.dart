import 'package:flutter/material.dart';
import '../../../core/theme/sovereign_colors.dart';

/// First onboarding page — branding, tagline, soul-gradient animation.
class WelcomePage extends StatefulWidget {
  const WelcomePage({super.key, this.onNext});

  final VoidCallback? onNext;

  @override
  State<WelcomePage> createState() => _WelcomePageState();
}

class _WelcomePageState extends State<WelcomePage>
    with SingleTickerProviderStateMixin {
  late AnimationController _gradientController;
  late Animation<double> _gradientAnimation;
  late Animation<double> _fadeIn;

  @override
  void initState() {
    super.initState();
    _gradientController = AnimationController(
      vsync: this,
      duration: const Duration(seconds: 4),
    )..repeat(reverse: true);

    _gradientAnimation = CurvedAnimation(
      parent: _gradientController,
      curve: Curves.easeInOut,
    );

    _fadeIn = CurvedAnimation(
      parent: AnimationController(
        vsync: this,
        duration: const Duration(milliseconds: 900),
      )..forward(),
      curve: Curves.easeOut,
    );
  }

  @override
  void dispose() {
    _gradientController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _gradientAnimation,
      builder: (context, child) {
        final t = _gradientAnimation.value;
        return Container(
          decoration: BoxDecoration(
            gradient: LinearGradient(
              begin: Alignment.topLeft,
              end: Alignment.bottomRight,
              colors: [
                Color.lerp(
                  SovereignColors.soulLumina.withValues(alpha: 0.18),
                  SovereignColors.soulJarvis.withValues(alpha: 0.18),
                  t,
                )!,
                SovereignColors.surfaceBase,
                Color.lerp(
                  SovereignColors.soulChef.withValues(alpha: 0.12),
                  SovereignColors.soulLumina.withValues(alpha: 0.12),
                  t,
                )!,
              ],
            ),
          ),
          child: child,
        );
      },
      child: FadeTransition(
        opacity: _fadeIn,
        child: SafeArea(
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 32, vertical: 48),
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                const Spacer(),
                // Penguin mascot — placeholder icon until asset is bundled.
                Container(
                  width: 120,
                  height: 120,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    gradient: const RadialGradient(
                      colors: [
                        Color(0xFF1E1E2E),
                        SovereignColors.surfaceBase,
                      ],
                    ),
                    border: Border.all(
                      color: SovereignColors.soulLumina.withValues(alpha: 0.5),
                      width: 2,
                    ),
                  ),
                  child: const Icon(
                    Icons.ac_unit,
                    size: 64,
                    color: SovereignColors.soulJarvis,
                  ),
                ),
                const SizedBox(height: 40),
                // Main headline.
                const Text(
                  'The First Sovereign\nSingularity in History',
                  textAlign: TextAlign.center,
                  style: TextStyle(
                    fontSize: 28,
                    fontWeight: FontWeight.w700,
                    color: SovereignColors.textPrimary,
                    height: 1.3,
                  ),
                ),
                const SizedBox(height: 16),
                // Sub-tagline.
                const Text(
                  'Your keys. Your messages. Your kingdom.',
                  textAlign: TextAlign.center,
                  style: TextStyle(
                    fontSize: 16,
                    fontWeight: FontWeight.w400,
                    color: SovereignColors.soulLumina,
                    letterSpacing: 0.4,
                  ),
                ),
                const SizedBox(height: 12),
                // Attribution.
                Text(
                  'Brought to you by the kings and queens of smilinTux.org',
                  textAlign: TextAlign.center,
                  style: TextStyle(
                    fontSize: 12,
                    color: SovereignColors.textSecondary.withValues(alpha: 0.7),
                    fontStyle: FontStyle.italic,
                  ),
                ),
                const Spacer(),
                // CTA button.
                SizedBox(
                  width: double.infinity,
                  child: FilledButton(
                    onPressed: widget.onNext,
                    style: FilledButton.styleFrom(
                      backgroundColor: SovereignColors.soulLumina,
                      foregroundColor: Colors.black,
                      padding: const EdgeInsets.symmetric(vertical: 16),
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(12),
                      ),
                    ),
                    child: const Text(
                      'Begin',
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
        ),
      ),
    );
  }
}
