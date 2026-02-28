import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:mobile_scanner/mobile_scanner.dart';

import '../../core/theme/glass_widgets.dart';
import '../../core/theme/sovereign_colors.dart';
import '../../services/capauth_service.dart';
import 'capauth_provider.dart';

// ── Screen ────────────────────────────────────────────────────────────────

/// Standalone QR login screen.
///
/// State machine:  scan → confirm → loading → success | error
///
/// The user scans a capauth:// QR code. If valid, they confirm the server
/// details and trigger the PGP challenge-response flow (biometric-gated).
class QrLoginScreen extends ConsumerStatefulWidget {
  const QrLoginScreen({super.key});

  @override
  ConsumerState<QrLoginScreen> createState() => _QrLoginScreenState();
}

class _QrLoginScreenState extends ConsumerState<QrLoginScreen> {
  _Step _step = _Step.scan;
  CapAuthQrPayload? _payload;
  String? _errorMessage;

  late final MobileScannerController _scanner = MobileScannerController(
    detectionSpeed: DetectionSpeed.noDuplicates,
  );

  @override
  void dispose() {
    _scanner.dispose();
    super.dispose();
  }

  // ── Scanner callback ────────────────────────────────────────────────────

  void _onDetect(BarcodeCapture capture) {
    if (_step != _Step.scan) return;
    final raw = capture.barcodes.firstOrNull?.rawValue;
    if (raw == null) return;

    final payload = CapAuthQrPayload.tryParse(raw);
    if (payload == null) {
      setState(() {
        _errorMessage = 'Not a CapAuth QR code.\n'
            'Expected capauth:// or https:// login URI.';
        _step = _Step.error;
      });
      return;
    }
    setState(() {
      _payload = payload;
      _step = _Step.confirm;
    });
  }

  // ── Confirm → login ──────────────────────────────────────────────────────

  Future<void> _confirm() async {
    final payload = _payload;
    if (payload == null) return;

    setState(() => _step = _Step.loading);

    final ok = await ref.read(capAuthProvider.notifier).loginWithQr(payload);
    if (!mounted) return;

    if (ok) {
      setState(() => _step = _Step.success);
    } else {
      final err = ref.read(capAuthProvider).error;
      setState(() {
        _errorMessage = err ?? 'Authentication failed.';
        _step = _Step.error;
      });
    }
  }

  void _retry() {
    setState(() {
      _step = _Step.scan;
      _payload = null;
      _errorMessage = null;
    });
  }

  // ── Build ────────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: SovereignColors.surfaceBase,
      appBar: AppBar(
        backgroundColor: Colors.transparent,
        elevation: 0,
        title: const Text('CapAuth Login'),
        leading: IconButton(
          icon: const Icon(Icons.close),
          onPressed: () => context.pop(),
        ),
      ),
      body: _buildBody(),
    );
  }

  Widget _buildBody() {
    switch (_step) {
      case _Step.scan:
        return _ScanView(controller: _scanner, onDetect: _onDetect);
      case _Step.confirm:
        return _ConfirmView(
          payload: _payload!,
          onConfirm: _confirm,
          onCancel: () => setState(() {
            _step = _Step.scan;
            _payload = null;
          }),
        );
      case _Step.loading:
        return const _LoadingView();
      case _Step.success:
        return _SuccessView(
          session: ref.watch(capAuthProvider).session,
          onDone: () => context.pop(),
        );
      case _Step.error:
        return _ErrorView(message: _errorMessage ?? 'Unknown error', onRetry: _retry);
    }
  }
}

enum _Step { scan, confirm, loading, success, error }

// ── Sub-views ─────────────────────────────────────────────────────────────

class _ScanView extends StatelessWidget {
  const _ScanView({required this.controller, required this.onDetect});

  final MobileScannerController controller;
  final void Function(BarcodeCapture) onDetect;

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        Expanded(
          child: MobileScanner(controller: controller, onDetect: onDetect),
        ),
        // Scanner hint
        const SafeArea(
          child: Padding(
            padding: EdgeInsets.symmetric(horizontal: 32, vertical: 20),
            child: Column(
              children: [
                Icon(Icons.qr_code_scanner, color: SovereignColors.soulLumina, size: 28),
                SizedBox(height: 8),
                Text(
                  'Point at a CapAuth QR code',
                  textAlign: TextAlign.center,
                  style: TextStyle(
                    color: SovereignColors.textSecondary,
                    fontSize: 14,
                  ),
                ),
              ],
            ),
          ),
        ),
      ],
    );
  }
}

class _ConfirmView extends StatelessWidget {
  const _ConfirmView({
    required this.payload,
    required this.onConfirm,
    required this.onCancel,
  });

  final CapAuthQrPayload payload;
  final VoidCallback onConfirm;
  final VoidCallback onCancel;

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const SizedBox(height: 16),
            const Icon(
              Icons.verified_user_rounded,
              size: 60,
              color: SovereignColors.soulLumina,
            ),
            const SizedBox(height: 20),
            const Text(
              'Login Request',
              textAlign: TextAlign.center,
              style: TextStyle(
                fontSize: 24,
                fontWeight: FontWeight.w700,
                color: SovereignColors.textPrimary,
              ),
            ),
            const SizedBox(height: 6),
            const Text(
              'Sign with your sovereign PGP key to authenticate.',
              textAlign: TextAlign.center,
              style: TextStyle(
                fontSize: 13,
                color: SovereignColors.textSecondary,
                height: 1.5,
              ),
            ),
            const SizedBox(height: 24),
            GlassCard(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  _InfoRow(label: 'Server', value: payload.server),
                  if (payload.fingerprint != null) ...[
                    const SizedBox(height: 10),
                    _InfoRow(label: 'Fingerprint', value: payload.fingerprint!),
                  ],
                  const SizedBox(height: 10),
                  _InfoRow(
                    label: 'Nonce',
                    value:
                        '${payload.nonce.substring(0, payload.nonce.length.clamp(0, 16))}…',
                  ),
                ],
              ),
            ),
            const SizedBox(height: 16),
            const Row(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                Icon(
                  Icons.fingerprint,
                  color: SovereignColors.accentEncrypt,
                  size: 15,
                ),
                SizedBox(width: 6),
                Text(
                  'Biometric required to sign',
                  style: TextStyle(
                    fontSize: 12,
                    color: SovereignColors.accentEncrypt,
                  ),
                ),
              ],
            ),
            const Spacer(),
            FilledButton(
              onPressed: onConfirm,
              style: FilledButton.styleFrom(
                backgroundColor: SovereignColors.soulLumina,
                foregroundColor: Colors.black,
                padding: const EdgeInsets.symmetric(vertical: 16),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12),
                ),
              ),
              child: const Text(
                'Sign & Login',
                style: TextStyle(fontSize: 16, fontWeight: FontWeight.w700),
              ),
            ),
            const SizedBox(height: 12),
            OutlinedButton(
              onPressed: onCancel,
              style: OutlinedButton.styleFrom(
                foregroundColor: SovereignColors.textSecondary,
                side:
                    const BorderSide(color: SovereignColors.surfaceGlassBorder),
                padding: const EdgeInsets.symmetric(vertical: 14),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12),
                ),
              ),
              child: const Text('Cancel'),
            ),
            const SizedBox(height: 24),
          ],
        ),
      ),
    );
  }
}

class _InfoRow extends StatelessWidget {
  const _InfoRow({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          '$label:  ',
          style: const TextStyle(
            fontSize: 12,
            color: SovereignColors.textTertiary,
          ),
        ),
        Expanded(
          child: Text(
            value,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(
              fontSize: 12,
              color: SovereignColors.textPrimary,
              fontFamily: 'monospace',
            ),
          ),
        ),
      ],
    );
  }
}

class _LoadingView extends StatelessWidget {
  const _LoadingView();

  @override
  Widget build(BuildContext context) {
    return const Center(
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          CircularProgressIndicator(
            color: SovereignColors.soulLumina,
            strokeWidth: 2,
          ),
          SizedBox(height: 24),
          Text(
            'Signing challenge…',
            style: TextStyle(
              color: SovereignColors.textSecondary,
              fontSize: 14,
            ),
          ),
        ],
      ),
    );
  }
}

class _SuccessView extends StatelessWidget {
  const _SuccessView({this.session, required this.onDone});

  final CapAuthSession? session;
  final VoidCallback onDone;

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const Icon(
              Icons.verified_rounded,
              size: 80,
              color: SovereignColors.accentEncrypt,
            ),
            const SizedBox(height: 24),
            const Text(
              'Authenticated',
              textAlign: TextAlign.center,
              style: TextStyle(
                fontSize: 28,
                fontWeight: FontWeight.w700,
                color: SovereignColors.textPrimary,
              ),
            ),
            if (session != null) ...[
              const SizedBox(height: 8),
              Text(
                session!.server,
                textAlign: TextAlign.center,
                style: const TextStyle(
                  fontSize: 13,
                  color: SovereignColors.textSecondary,
                ),
              ),
            ],
            const SizedBox(height: 48),
            FilledButton(
              onPressed: onDone,
              style: FilledButton.styleFrom(
                backgroundColor: SovereignColors.accentEncrypt,
                foregroundColor: Colors.black,
                padding: const EdgeInsets.symmetric(vertical: 16),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12),
                ),
              ),
              child: const Text(
                'Done',
                style: TextStyle(fontSize: 16, fontWeight: FontWeight.w700),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _ErrorView extends StatelessWidget {
  const _ErrorView({required this.message, required this.onRetry});

  final String message;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const Icon(
              Icons.error_outline_rounded,
              size: 72,
              color: SovereignColors.accentDanger,
            ),
            const SizedBox(height: 24),
            const Text(
              'Login Failed',
              textAlign: TextAlign.center,
              style: TextStyle(
                fontSize: 24,
                fontWeight: FontWeight.w700,
                color: SovereignColors.textPrimary,
              ),
            ),
            const SizedBox(height: 12),
            Text(
              message,
              textAlign: TextAlign.center,
              style: const TextStyle(
                fontSize: 13,
                color: SovereignColors.textSecondary,
                height: 1.5,
              ),
            ),
            const SizedBox(height: 48),
            FilledButton(
              onPressed: onRetry,
              style: FilledButton.styleFrom(
                backgroundColor: SovereignColors.soulLumina,
                foregroundColor: Colors.black,
                padding: const EdgeInsets.symmetric(vertical: 16),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12),
                ),
              ),
              child: const Text(
                'Try Again',
                style: TextStyle(fontSize: 16, fontWeight: FontWeight.w700),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
