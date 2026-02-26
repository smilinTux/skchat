import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../../core/theme/sovereign_colors.dart';
import '../../../core/theme/glass_widgets.dart';
import '../onboarding_provider.dart';

/// Onboarding step 3 — detect SKComm daemon and Syncthing availability.
class TransportPage extends ConsumerStatefulWidget {
  const TransportPage({super.key, this.onNext});

  final VoidCallback? onNext;

  @override
  ConsumerState<TransportPage> createState() => _TransportPageState();
}

class _TransportPageState extends ConsumerState<TransportPage> {
  bool _hasRun = false;

  @override
  void initState() {
    super.initState();
    // Auto-probe on first build.
    Future.microtask(_probe);
  }

  Future<void> _probe() async {
    await ref.read(onboardingProvider.notifier).detectTransports();
    _hasRun = true;
  }

  @override
  Widget build(BuildContext context) {
    final state = ref.watch(onboardingProvider);

    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 32),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const SizedBox(height: 16),
            const Text(
              'Transport Layer',
              textAlign: TextAlign.center,
              style: TextStyle(
                fontSize: 26,
                fontWeight: FontWeight.w700,
                color: SovereignColors.textPrimary,
              ),
            ),
            const SizedBox(height: 8),
            const Text(
              'SKChat routes messages through local daemons.\nLet\'s see what\'s running.',
              textAlign: TextAlign.center,
              style: TextStyle(
                fontSize: 14,
                color: SovereignColors.textSecondary,
                height: 1.5,
              ),
            ),
            const SizedBox(height: 32),
            _TransportRow(
              icon: Icons.hub,
              label: 'SKComm Daemon',
              description: 'Sovereign peer-to-peer message bus',
              detected: state.daemonDetected,
              loading: state.isDetecting,
              setupHint: 'Run: systemctl --user start skcomm',
            ),
            const SizedBox(height: 16),
            _TransportRow(
              icon: Icons.sync,
              label: 'Syncthing',
              description: 'Distributed file-based sync transport',
              detected: state.syncthingDetected,
              loading: state.isDetecting,
              setupHint: 'Install Syncthing and ensure it\'s running.',
            ),
            const SizedBox(height: 24),
            if (_hasRun && !state.isDetecting)
              TextButton.icon(
                onPressed: _probe,
                icon: const Icon(
                  Icons.refresh,
                  size: 16,
                  color: SovereignColors.textSecondary,
                ),
                label: const Text(
                  'Re-check',
                  style: TextStyle(color: SovereignColors.textSecondary),
                ),
              ),
            const Spacer(),
            FilledButton(
              onPressed: state.isDetecting ? null : widget.onNext,
              style: FilledButton.styleFrom(
                backgroundColor: SovereignColors.soulLumina,
                foregroundColor: Colors.black,
                disabledBackgroundColor: SovereignColors.surfaceGlass,
                padding: const EdgeInsets.symmetric(vertical: 16),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12),
                ),
              ),
              child: Text(
                (state.daemonDetected || state.syncthingDetected)
                    ? 'Continue'
                    : 'Continue anyway',
                style: const TextStyle(
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

class _TransportRow extends StatelessWidget {
  const _TransportRow({
    required this.icon,
    required this.label,
    required this.description,
    required this.detected,
    required this.loading,
    required this.setupHint,
  });

  final IconData icon;
  final String label;
  final String description;
  final bool detected;
  final bool loading;
  final String setupHint;

  @override
  Widget build(BuildContext context) {
    final statusColor = loading
        ? SovereignColors.textTertiary
        : detected
            ? SovereignColors.accentEncrypt
            : SovereignColors.accentWarning;

    return GlassCard(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(icon, size: 22, color: statusColor),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      label,
                      style: const TextStyle(
                        fontSize: 15,
                        fontWeight: FontWeight.w600,
                        color: SovereignColors.textPrimary,
                      ),
                    ),
                    Text(
                      description,
                      style: const TextStyle(
                        fontSize: 12,
                        color: SovereignColors.textSecondary,
                      ),
                    ),
                  ],
                ),
              ),
              if (loading)
                const SizedBox(
                  width: 18,
                  height: 18,
                  child: CircularProgressIndicator(
                    strokeWidth: 2,
                    color: SovereignColors.textTertiary,
                  ),
                )
              else
                Icon(
                  detected ? Icons.check_circle : Icons.warning_amber_rounded,
                  size: 22,
                  color: statusColor,
                ),
            ],
          ),
          if (!loading && !detected) ...[
            const SizedBox(height: 10),
            Container(
              padding:
                  const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
              decoration: BoxDecoration(
                color: SovereignColors.accentWarning.withValues(alpha: 0.08),
                borderRadius: BorderRadius.circular(8),
                border: Border.all(
                  color:
                      SovereignColors.accentWarning.withValues(alpha: 0.2),
                ),
              ),
              child: Row(
                children: [
                  const Icon(
                    Icons.info_outline,
                    size: 13,
                    color: SovereignColors.accentWarning,
                  ),
                  const SizedBox(width: 6),
                  Expanded(
                    child: Text(
                      setupHint,
                      style: const TextStyle(
                        fontSize: 11,
                        color: SovereignColors.accentWarning,
                        fontFamily: 'monospace',
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ],
        ],
      ),
    );
  }
}
