import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:mobile_scanner/mobile_scanner.dart';
import 'package:qr_flutter/qr_flutter.dart';
import '../../../core/theme/sovereign_colors.dart';
import '../../../core/theme/glass_widgets.dart';

/// Onboarding step 4 — display a QR code for this node and optionally scan
/// another device to establish an initial peer pairing.
class PairPage extends ConsumerStatefulWidget {
  const PairPage({super.key, this.onNext});

  final VoidCallback? onNext;

  @override
  ConsumerState<PairPage> createState() => _PairPageState();
}

class _PairPageState extends ConsumerState<PairPage> {
  bool _scanning = false;
  String? _scannedPeerId;

  /// Stub peer ID — replaced by the real CapAuth node ID once identity is wired.
  static const _stubPeerId =
      'skchat://peer/smilintux/00112233-4455-6677-8899-aabbccddeeff';

  void _startScan() => setState(() => _scanning = true);
  void _stopScan() => setState(() => _scanning = false);

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 32),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const SizedBox(height: 16),
            const Text(
              'Pair a Device',
              textAlign: TextAlign.center,
              style: TextStyle(
                fontSize: 26,
                fontWeight: FontWeight.w700,
                color: SovereignColors.textPrimary,
              ),
            ),
            const SizedBox(height: 8),
            const Text(
              'Show this QR on your other device, or scan\ntheirs to link your kingdom.',
              textAlign: TextAlign.center,
              style: TextStyle(
                fontSize: 14,
                color: SovereignColors.textSecondary,
                height: 1.5,
              ),
            ),
            const SizedBox(height: 28),
            if (_scanning) ...[
              _ScannerOverlay(
                onDetect: (barcodes) {
                  final value = barcodes.barcodes.firstOrNull?.rawValue;
                  if (value != null) {
                    setState(() {
                      _scannedPeerId = value;
                      _scanning = false;
                    });
                  }
                },
                onClose: _stopScan,
              ),
            ] else ...[
              // QR code display.
              Center(
                child: GlassCard(
                  padding: const EdgeInsets.all(20),
                  child: Column(
                    children: [
                      QrImageView(
                        data: _stubPeerId,
                        version: QrVersions.auto,
                        size: 200,
                        backgroundColor: Colors.white,
                        errorCorrectionLevel: QrErrorCorrectLevel.M,
                      ),
                      const SizedBox(height: 12),
                      const Text(
                        'This node\'s peer ID',
                        style: TextStyle(
                          fontSize: 12,
                          color: SovereignColors.textSecondary,
                        ),
                      ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 20),
              if (_scannedPeerId != null)
                GlassCard(
                  padding: const EdgeInsets.all(12),
                  child: Row(
                    children: [
                      const Icon(
                        Icons.check_circle,
                        color: SovereignColors.accentEncrypt,
                        size: 18,
                      ),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          'Paired: $_scannedPeerId',
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            fontSize: 12,
                            color: SovereignColors.textPrimary,
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
              const SizedBox(height: 16),
              OutlinedButton.icon(
                onPressed: _startScan,
                icon: const Icon(Icons.qr_code_scanner),
                label: const Text('Scan another device\'s QR'),
                style: OutlinedButton.styleFrom(
                  foregroundColor: SovereignColors.soulJarvis,
                  side: const BorderSide(
                    color: SovereignColors.soulJarvis,
                    width: 1,
                  ),
                  padding: const EdgeInsets.symmetric(vertical: 14),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(12),
                  ),
                ),
              ),
            ],
            const Spacer(),
            FilledButton(
              onPressed: widget.onNext,
              style: FilledButton.styleFrom(
                backgroundColor: SovereignColors.soulLumina,
                foregroundColor: Colors.black,
                padding: const EdgeInsets.symmetric(vertical: 16),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12),
                ),
              ),
              child: Text(
                _scannedPeerId != null ? 'Continue' : 'Skip for now',
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

/// Inline camera scanner overlay shown when the user taps "Scan".
class _ScannerOverlay extends StatefulWidget {
  const _ScannerOverlay({
    required this.onDetect,
    required this.onClose,
  });

  final void Function(BarcodeCapture) onDetect;
  final VoidCallback onClose;

  @override
  State<_ScannerOverlay> createState() => _ScannerOverlayState();
}

class _ScannerOverlayState extends State<_ScannerOverlay> {
  late final MobileScannerController _controller;

  @override
  void initState() {
    super.initState();
    _controller = MobileScannerController(
      detectionSpeed: DetectionSpeed.noDuplicates,
    );
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return ClipRRect(
      borderRadius: BorderRadius.circular(16),
      child: SizedBox(
        height: 280,
        child: Stack(
          children: [
            MobileScanner(
              controller: _controller,
              onDetect: widget.onDetect,
            ),
            // Close button overlay.
            Positioned(
              top: 8,
              right: 8,
              child: GestureDetector(
                onTap: widget.onClose,
                child: Container(
                  decoration: BoxDecoration(
                    color: Colors.black.withValues(alpha: 0.6),
                    shape: BoxShape.circle,
                  ),
                  padding: const EdgeInsets.all(6),
                  child: const Icon(
                    Icons.close,
                    color: Colors.white,
                    size: 18,
                  ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
