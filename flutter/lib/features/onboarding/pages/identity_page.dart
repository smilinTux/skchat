import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../../core/crypto/pgp_bridge.dart';
import '../../../core/theme/sovereign_colors.dart';
import '../../../core/theme/glass_widgets.dart';
import '../../../services/identity_service.dart';
import '../onboarding_provider.dart';

/// Onboarding step 2 — import an existing PGP key or generate a new identity.
///
/// On first render, checks [identityKeyPairProvider] for a key already stored
/// in the OS keychain (e.g. from a previous install) and pre-fills the UI.
/// After generation or import the key is persisted via [IdentityService].
class IdentityPage extends ConsumerStatefulWidget {
  const IdentityPage({super.key, this.onNext});

  final VoidCallback? onNext;

  @override
  ConsumerState<IdentityPage> createState() => _IdentityPageState();
}

class _IdentityPageState extends ConsumerState<IdentityPage> {
  bool _generating = false;
  bool _importing = false;

  @override
  void initState() {
    super.initState();
    // Load any persisted key and pre-fill onboarding state.
    Future.microtask(_loadExistingKey);
  }

  /// If a key already lives in secure storage, restore it into [OnboardingNotifier]
  /// so the "Continue" button is enabled without the user having to re-generate.
  Future<void> _loadExistingKey() async {
    final existing = await ref.read(identityServiceProvider).load();
    if (existing == null || !mounted) return;
    await ref.read(onboardingProvider.notifier).setIdentityChoice(
          'generate',
          fingerprint: existing.fingerprint,
        );
  }

  /// Generate a fresh RSA-2048 keypair, persist it, and update onboarding state.
  Future<void> _generateIdentity() async {
    setState(() => _generating = true);
    try {
      final keyPair = await PgpBridge.generateKeyPair();

      // Persist both keys to the OS keychain.
      await ref.read(identityServiceProvider).save(keyPair);

      await ref.read(onboardingProvider.notifier).setIdentityChoice(
            'generate',
            fingerprint: keyPair.fingerprint,
          );
    } on Exception catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Key generation failed: $e')),
        );
      }
    } finally {
      if (mounted) setState(() => _generating = false);
    }
  }

  /// Show a dialog for the user to paste their PEM private key, then import
  /// and persist it.
  Future<void> _importKey() async {
    final pem = await showDialog<String>(
      context: context,
      builder: (ctx) => _ImportKeyDialog(),
    );
    if (pem == null || !mounted) return;

    setState(() => _importing = true);
    try {
      final keyPair = PgpBridge.importPrivateKey(pem);

      // Persist both keys to the OS keychain.
      await ref.read(identityServiceProvider).save(keyPair);

      await ref.read(onboardingProvider.notifier).setIdentityChoice(
            'import',
            fingerprint: keyPair.fingerprint,
          );
    } on Exception catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Import failed: $e')),
        );
      }
    } finally {
      if (mounted) setState(() => _importing = false);
    }
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
              subtitle: 'Paste your RSA private key (PEM format).',
              selected: chosen == 'import',
              loading: _importing,
              onTap: chosen == null || chosen == 'import' ? _importKey : null,
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

// ── Import key dialog ─────────────────────────────────────────────────────────

class _ImportKeyDialog extends StatefulWidget {
  @override
  State<_ImportKeyDialog> createState() => _ImportKeyDialogState();
}

class _ImportKeyDialogState extends State<_ImportKeyDialog> {
  final _ctrl = TextEditingController();
  String? _error;

  void _validate() {
    final text = _ctrl.text.trim();
    if (!text.contains('-----BEGIN RSA PRIVATE KEY-----') ||
        !text.contains('-----END RSA PRIVATE KEY-----')) {
      setState(() {
        _error = 'Must be a PKCS#1 PEM block '
            '(-----BEGIN RSA PRIVATE KEY-----)';
      });
      return;
    }
    try {
      PgpBridge.importPrivateKey(text); // dry-run parse
    } catch (_) {
      setState(() => _error = 'Could not parse the key — check formatting.');
      return;
    }
    Navigator.of(context).pop(text);
  }

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      backgroundColor: SovereignColors.surfaceCard,
      title: const Text(
        'Import PGP Key',
        style: TextStyle(color: SovereignColors.textPrimary),
      ),
      content: SizedBox(
        width: double.maxFinite,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text(
              'Paste your RSA private key in PEM format.',
              style: TextStyle(
                fontSize: 13,
                color: SovereignColors.textSecondary,
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _ctrl,
              maxLines: 10,
              style: const TextStyle(
                fontFamily: 'monospace',
                fontSize: 12,
                color: SovereignColors.textPrimary,
              ),
              decoration: InputDecoration(
                hintText: '-----BEGIN RSA PRIVATE KEY-----\n...',
                hintStyle: const TextStyle(
                  color: SovereignColors.textSecondary,
                  fontSize: 12,
                ),
                filled: true,
                fillColor: SovereignColors.surfaceGlass,
                border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(8),
                  borderSide: BorderSide.none,
                ),
                errorText: _error,
              ),
              onChanged: (_) {
                if (_error != null) setState(() => _error = null);
              },
            ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text(
            'Cancel',
            style: TextStyle(color: SovereignColors.textSecondary),
          ),
        ),
        FilledButton(
          onPressed: _validate,
          style: FilledButton.styleFrom(
            backgroundColor: SovereignColors.soulJarvis,
            foregroundColor: Colors.black,
          ),
          child: const Text('Import'),
        ),
      ],
    );
  }
}

// ── Option card ───────────────────────────────────────────────────────────────

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
