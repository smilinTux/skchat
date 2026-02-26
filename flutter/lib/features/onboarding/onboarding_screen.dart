import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../core/theme/sovereign_colors.dart';
import 'onboarding_provider.dart';
import 'pages/welcome_page.dart';
import 'pages/identity_page.dart';
import 'pages/transport_page.dart';
import 'pages/pair_page.dart';
import 'pages/complete_page.dart';

/// Main onboarding wizard — a horizontal PageView with 5 pages, animated dot
/// indicators, and Skip / Next navigation controls.
///
/// Pass [onComplete] to receive a callback when the user taps "Enter SKChat"
/// on the final page.
class OnboardingScreen extends ConsumerStatefulWidget {
  const OnboardingScreen({super.key, this.onComplete});

  final VoidCallback? onComplete;

  @override
  ConsumerState<OnboardingScreen> createState() => _OnboardingScreenState();
}

class _OnboardingScreenState extends ConsumerState<OnboardingScreen> {
  late final PageController _pageController;

  @override
  void initState() {
    super.initState();
    _pageController = PageController();
  }

  @override
  void dispose() {
    _pageController.dispose();
    super.dispose();
  }

  void _goToPage(int page) {
    _pageController.animateToPage(
      page,
      duration: const Duration(milliseconds: 350),
      curve: Curves.easeInOut,
    );
    ref.read(onboardingProvider.notifier).goToStep(page);
  }

  void _next() {
    final current = ref.read(onboardingProvider).currentStep;
    if (current < kOnboardingPageCount - 1) {
      _goToPage(current + 1);
    }
  }

  void _skip() {
    _goToPage(kOnboardingPageCount - 1);
  }

  @override
  Widget build(BuildContext context) {
    final step = ref.watch(onboardingProvider).currentStep;
    final isLastPage = step == kOnboardingPageCount - 1;

    return Scaffold(
      backgroundColor: SovereignColors.surfaceBase,
      body: Stack(
        children: [
          // Page content.
          PageView(
            controller: _pageController,
            onPageChanged: (index) {
              ref.read(onboardingProvider.notifier).goToStep(index);
            },
            children: [
              WelcomePage(onNext: _next),
              IdentityPage(onNext: _next),
              TransportPage(onNext: _next),
              PairPage(onNext: _next),
              CompletePage(onEnterChat: widget.onComplete),
            ],
          ),
          // Top navigation bar (skip button + step label).
          Positioned(
            top: 0,
            left: 0,
            right: 0,
            child: SafeArea(
              child: Padding(
                padding:
                    const EdgeInsets.symmetric(horizontal: 20, vertical: 8),
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    // Step counter.
                    Text(
                      '${step + 1} / $kOnboardingPageCount',
                      style: const TextStyle(
                        fontSize: 12,
                        color: SovereignColors.textTertiary,
                      ),
                    ),
                    // Skip only visible on non-last pages.
                    if (!isLastPage)
                      TextButton(
                        onPressed: _skip,
                        child: const Text(
                          'Skip',
                          style: TextStyle(
                            fontSize: 13,
                            color: SovereignColors.textSecondary,
                          ),
                        ),
                      ),
                  ],
                ),
              ),
            ),
          ),
          // Bottom dot indicator.
          Positioned(
            bottom: 0,
            left: 0,
            right: 0,
            child: SafeArea(
              child: Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: List.generate(
                    kOnboardingPageCount,
                    (i) => _DotIndicator(
                      active: i == step,
                      onTap: () => _goToPage(i),
                    ),
                  ),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// Individual animated dot for the page indicator.
class _DotIndicator extends StatelessWidget {
  const _DotIndicator({required this.active, this.onTap});

  final bool active;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 300),
        curve: Curves.easeInOut,
        margin: const EdgeInsets.symmetric(horizontal: 4),
        width: active ? 24 : 8,
        height: 8,
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(4),
          color: active
              ? SovereignColors.soulLumina
              : SovereignColors.textTertiary.withValues(alpha: 0.4),
        ),
      ),
    );
  }
}
