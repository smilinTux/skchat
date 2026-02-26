import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../../core/theme/sovereign_colors.dart';
import '../../../core/theme/glass_widgets.dart';
import '../onboarding_provider.dart';

/// Onboarding step 2 — import an existing PGP key or generate a new identity.
///
/// Actual PGP operations are stubbed; state is stored in [OnboardingNotifier].
class IdentityPage extends ConsumerStatefulWidget {
  const IdentityPage({super.key, this.onNext});

  final VoidCallback? onNext;

  @override
  ConsumerState<IdentityPage> createState() => _IdentityPageState();
}

class _IdentityPageState extends ConsumerState<IdentityPage> {
  bool _generating = false;

  /// Stub — simulates generating a new keypair and returning a fingerprint.
  Future<void> _generateIdentity() async {
    setState(() => _generating = true);
    // Actual PGP keygen will be wired in a later task.
    await Future.delayed(const Duration(milliseconds: 800));
    const stubFingerprint = 'A1B2 C3D4 E5F6 0718 29AA  BB3C DD4E EF56 7890 ABCD';
    await ref.read(onboardingProvider.notifier).setIdentityChoice(
          'generate',
          fingerprint: stubFingerprint,
        );
    setState(() => _generating = false);
  }

  Future<void> _importKey() async {
    // File-picker integration is wired in a later task; store choice for now.
    await ref
        .read(onboardingProvider.notifier)
        .setIdentityChoice('import');
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(onboardingProvider);
    final chosen = state.identityChoice;
    final fingerprint = state.generatedFingerprint;

    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 32),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const SizedBox(height: 16),
            const Text(
              'Your Identity',
              textAlign: TextAlign.center,
              style: TextStyle(
                fontSize: 26,
                fontWeight: FontWeight.w700,
                color: SovereignColors.textPrimary,
              ),
            ),
            const SizedBox(height: 8),
            const Text(
              'Sovereign identity is anchored to your PGP key.\nNo account. No server. Just math.',
              textAlign: TextAlign.center,
              style: TextStyle(
                fontSize: 14,
                color: SovereignColors.textSecondary,
                height: 1.5,
              ),
            ),
            const SizedBox(height: 32),
            // Option A — Generate new identity.
            _OptionCard(
              icon: Icons.auto_awesome,
              iconColor: SovereignColors.soulLumina,
              title: 'Generate new identity',
              subtitle: 'Create a fresh keypair on this device.',
              selected: chosen == 'generate',
              loading: _generating,
              onTap: chosen == null || chosen == 'generate'
                  ? _generateIdentity
                  : null,
            ),
            const SizedBox(height: 16),
            // Option B — Import existing PGP key.
            _OptionCard(
              icon: Icons.file_upload_outlined,
              iconColor: SovereignColors.soulJarvis,
              title: 'Import existing PGP key',
              subtitle: 'Load your key from a file or clipboard.',
              selected: chosen == 'import',
              onTap:
                  chosen == null || chosen == 'import' ? _importKey : null,
            ),
            const SizedBox(height: 24),
            // Fingerprint display.
            if (fingerprint != null) ...[
              GlassCard(
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        const Icon(
                          Icons.fingerprint,
                          size: 16,
                          color: SovereignColors.accentEncrypt,
                        ),
                        const SizedBox(width: 6),
                        const Text(
                          'Fingerprint',
                          style: TextStyle(
                            fontSize: 12,
                            fontWeight: FontWeight.w600,
                            color: SovereignColors.accentEncrypt,
                          ),
                        ),
                        const Spacer(),
                        GestureDetector(
                          onTap: () {
                            Clipboard.setData(
                              ClipboardData(text: fingerprint),
                            );
                            ScaffoldMessenger.of(context).showSnackBar(
                              const SnackBar(
                                content: Text('Fingerprint copied'),
                                duration: Duration(seconds: 2),
                              ),
                            );
                          },
                          child: const Icon(
                            Icons.copy,
                            size: 16,
                            color: SovereignColors.textSecondary,
                          ),
                        ),
                      ],
                    ),
                    const SizedBox(height: 8),
                    Text(
                      fingerprint,
                      style: const TextStyle(
                        fontFamily: 'monospace',
                        fontSize: 13,
                        color: SovereignColors.textPrimary,
                        letterSpacing: 1.2,
                      ),
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 24),
            ],
            const Spacer(),
            FilledButton(
              onPressed: chosen != null ? widget.onNext : null,
              style: FilledButton.styleFrom(
                backgroundColor: SovereignColors.soulLumina,
                foregroundColor: Colors.black,
                disabledBackgroundColor:
                    SovereignColors.surfaceGlass,
                padding: const EdgeInsets.symmetric(vertical: 16),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12),
                ),
              ),
              child: const Text(
                'Continue',
                style: TextStyle(
                  fontSize: 16,
                  fontWeight: FontWeight.w700,
                ),
              ),
            ),
            const SizedBox(height: 16),
          ],
        ),
      ),
    );
  }
}

class _OptionCard extends StatelessWidget {
  const _OptionCard({
    required this.icon,
    required this.iconColor,
    required this.title,
    required this.subtitle,
    required this.selected,
    this.loading = false,
    this.onTap,
  });

  final IconData icon;
  final Color iconColor;
  final String title;
  final String subtitle;
  final bool selected;
  final bool loading;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    return GlassCard(
      opacity: selected ? 0.12 : 0.06,
      borderOpacity: selected ? 0.25 : 0.08,
      onTap: onTap,
      child: Row(
        children: [
          Container(
            width: 44,
            height: 44,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: iconColor.withValues(alpha: 0.15),
            ),
            child: loading
                ? Padding(
                    padding: const EdgeInsets.all(10),
                    child: CircularProgressIndicator(
                      strokeWidth: 2,
                      color: iconColor,
                    ),
                  )
                : Icon(icon, color: iconColor, size: 22),
          ),
          const SizedBox(width: 16),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  title,
                  style: const TextStyle(
                    fontSize: 15,
                    fontWeight: FontWeight.w600,
                    color: SovereignColors.textPrimary,
                  ),
                ),
                const SizedBox(height: 2),
                Text(
                  subtitle,
                  style: const TextStyle(
                    fontSize: 12,
                    color: SovereignColors.textSecondary,
                  ),
                ),
              ],
            ),
          ),
          if (selected)
            const Icon(
              Icons.check_circle,
              color: SovereignColors.accentEncrypt,
              size: 20,
            ),
        ],
      ),
    );
  }
}
