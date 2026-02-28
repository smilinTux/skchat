import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:mobile_scanner/mobile_scanner.dart';
import 'package:qr_flutter/qr_flutter.dart';
import '../../core/theme/theme.dart';
import 'profile_screen.dart';

// ── QR login screen ────────────────────────────────────────────────────────

/// QR code login/pairing screen.
/// Two tabs: "My QR" (show this node's peer ID as QR) and "Scan" (camera scanner).
/// Accessible from the Profile screen and from the nav bar QR icon.
class QrLoginScreen extends ConsumerStatefulWidget {
  const QrLoginScreen({super.key});

  @override
  ConsumerState<QrLoginScreen> createState() => _QrLoginScreenState();
}

class _QrLoginScreenState extends ConsumerState<QrLoginScreen>
    with SingleTickerProviderStateMixin {
  late final TabController _tabController;
  String? _scannedValue;
  bool _scanning = false;
  _ScanStatus _scanStatus = _ScanStatus.idle;

  @override
  void initState() {
    super.initState();
    _tabController = TabController(length: 2, vsync: this);
    _tabController.addListener(() {
      if (_tabController.indexIsChanging) {
        setState(() {
          _scanning = false;
          _scanStatus = _ScanStatus.idle;
        });
      }
    });
  }

  @override
  void dispose() {
    _tabController.dispose();
    super.dispose();
  }

  void _onScanned(String value) {
    setState(() {
      _scannedValue = value;
      _scanning = false;
      _scanStatus = _ScanStatus.success;
    });
  }

  void _reset() {
    setState(() {
      _scannedValue = null;
      _scanning = false;
      _scanStatus = _ScanStatus.idle;
    });
  }

  @override
  Widget build(BuildContext context) {
    final identity = ref.watch(localIdentityProvider);
    final soulColor = SovereignColors.fromFingerprint(identity.fingerprint);
    final tt = Theme.of(context).textTheme;

    // Build this node's peer URI to embed in the QR code.
    final peerUri = 'skchat://peer/${identity.displayName.toLowerCase()}/'
        '${identity.fingerprint}';

    return Scaffold(
      backgroundColor: SovereignColors.surfaceBase,
      appBar: AppBar(
        backgroundColor: SovereignColors.surfaceBase,
        title: Text('QR Login', style: tt.titleLarge),
        bottom: TabBar(
          controller: _tabController,
          tabs: const [
            Tab(text: 'My QR Code'),
            Tab(text: 'Scan'),
          ],
          labelColor: SovereignColors.soulLumina,
          unselectedLabelColor: SovereignColors.textSecondary,
          indicatorColor: SovereignColors.soulLumina,
          dividerColor: SovereignColors.surfaceGlassBorder,
        ),
      ),
      body: TabBarView(
        controller: _tabController,
        children: [
          _MyQrTab(
            peerUri: peerUri,
            identity: identity,
            soulColor: soulColor,
          ),
          _ScanTab(
            scannedValue: _scannedValue,
            scanStatus: _scanStatus,
            scanning: _scanning,
            soulColor: soulColor,
            onStartScan: () => setState(() {
              _scanning = true;
              _scanStatus = _ScanStatus.idle;
            }),
            onScanned: _onScanned,
            onReset: _reset,
            onConnect: () => _connectToPeer(context, _scannedValue),
          ),
        ],
      ),
    );
  }

  void _connectToPeer(BuildContext context, String? uri) {
    if (uri == null) return;
    // Parse the scanned skchat:// URI and attempt to initiate a connection.
    // In the full implementation this would call SKCommClient.addPeer().
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text('Pairing with: ${_extractName(uri)}'),
        backgroundColor: SovereignColors.accentEncrypt,
      ),
    );
  }

  String _extractName(String uri) {
    // skchat://peer/<name>/<fingerprint> → <name>
    try {
      final parts = uri.replaceFirst('skchat://peer/', '').split('/');
      return parts.first;
    } catch (_) {
      return uri;
    }
  }
}

enum _ScanStatus { idle, success, error }

// ── My QR tab ─────────────────────────────────────────────────────────────

class _MyQrTab extends StatelessWidget {
  const _MyQrTab({
    required this.peerUri,
    required this.identity,
    required this.soulColor,
  });

  final String peerUri;
  final LocalIdentity identity;
  final Color soulColor;

  @override
  Widget build(BuildContext context) {
    final tt = Theme.of(context).textTheme;

    return SingleChildScrollView(
      padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 32),
      child: Column(
        children: [
          // Soul-color glow QR card
          Center(
            child: Container(
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(24),
                boxShadow: [
                  BoxShadow(
                    color: soulColor.withValues(alpha: 0.25),
                    blurRadius: 40,
                    spreadRadius: 4,
                  ),
                ],
              ),
              child: GlassCard(
                padding: const EdgeInsets.all(24),
                child: Column(
                  children: [
                    // QR code with soul-color finder modules
                    QrImageView(
                      data: peerUri,
                      version: QrVersions.auto,
                      size: 220,
                      backgroundColor: Colors.white,
                      errorCorrectionLevel: QrErrorCorrectLevel.M,
                      eyeStyle: QrEyeStyle(
                        eyeShape: QrEyeShape.square,
                        color: HSLColor.fromColor(soulColor)
                            .withLightness(0.3)
                            .toColor(),
                      ),
                    ),
                    const SizedBox(height: 16),
                    Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        const EncryptBadge(size: 12),
                        const SizedBox(width: 6),
                        Text(
                          identity.displayName,
                          style: tt.titleSmall?.copyWith(
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                      ],
                    ),
                    const SizedBox(height: 4),
                    Text(
                      'Sovereign peer ID',
                      style: tt.labelSmall?.copyWith(
                        color: SovereignColors.textTertiary,
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
          const SizedBox(height: 24),

          // Instructions
          GlassCard(
            padding: const EdgeInsets.all(16),
            child: Column(
              children: [
                _Step(
                  number: 1,
                  text: 'Show this QR to another SKChat device.',
                  soulColor: soulColor,
                ),
                const SizedBox(height: 10),
                _Step(
                  number: 2,
                  text: 'They scan it with the Scan tab to pair.',
                  soulColor: soulColor,
                ),
                const SizedBox(height: 10),
                _Step(
                  number: 3,
                  text: 'Messages are end-to-end encrypted automatically.',
                  soulColor: soulColor,
                ),
              ],
            ),
          ),
          const SizedBox(height: 20),

          // Copy URI button
          OutlinedButton.icon(
            onPressed: () {
              Clipboard.setData(ClipboardData(text: peerUri));
              ScaffoldMessenger.of(context).showSnackBar(
                const SnackBar(
                  content: Text('Peer URI copied to clipboard'),
                  duration: Duration(seconds: 2),
                ),
              );
            },
            icon: const Icon(Icons.copy_rounded, size: 16),
            label: const Text('Copy peer URI'),
            style: OutlinedButton.styleFrom(
              foregroundColor: soulColor,
              side: BorderSide(color: soulColor.withValues(alpha: 0.5)),
              padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 12),
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(12),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

// ── Scan tab ───────────────────────────────────────────────────────────────

class _ScanTab extends StatelessWidget {
  const _ScanTab({
    required this.scannedValue,
    required this.scanStatus,
    required this.scanning,
    required this.soulColor,
    required this.onStartScan,
    required this.onScanned,
    required this.onReset,
    required this.onConnect,
  });

  final String? scannedValue;
  final _ScanStatus scanStatus;
  final bool scanning;
  final Color soulColor;
  final VoidCallback onStartScan;
  final void Function(String) onScanned;
  final VoidCallback onReset;
  final VoidCallback onConnect;

  @override
  Widget build(BuildContext context) {
    final tt = Theme.of(context).textTheme;

    if (scanning) {
      return _LiveScanner(
        onDetect: (capture) {
          final value = capture.barcodes.firstOrNull?.rawValue;
          if (value != null) onScanned(value);
        },
        onClose: onReset,
        soulColor: soulColor,
      );
    }

    return SingleChildScrollView(
      padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 32),
      child: Column(
        children: [
          if (scanStatus == _ScanStatus.success && scannedValue != null) ...[
            // Success state
            _ScanResult(
              value: scannedValue!,
              soulColor: soulColor,
              onConnect: onConnect,
              onReset: onReset,
            ),
          ] else ...[
            // Idle state — scanner invite
            Container(
              width: double.infinity,
              height: 260,
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(20),
                border: Border.all(
                  color: SovereignColors.surfaceGlassBorder,
                  width: 1,
                ),
              ),
              child: ClipRRect(
                borderRadius: BorderRadius.circular(20),
                child: Stack(
                  alignment: Alignment.center,
                  children: [
                    // Dashed guide frame
                    CustomPaint(
                      size: const Size(double.infinity, 260),
                      painter: _ScanFramePainter(color: soulColor),
                    ),
                    Column(
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: [
                        Icon(
                          Icons.qr_code_scanner_rounded,
                          size: 64,
                          color: soulColor.withValues(alpha: 0.4),
                        ),
                        const SizedBox(height: 12),
                        Text(
                          'Tap to start camera',
                          style: tt.bodyMedium?.copyWith(
                            color: SovereignColors.textSecondary,
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            ),
            const SizedBox(height: 24),

            // Instructions
            GlassCard(
              padding: const EdgeInsets.all(16),
              child: Column(
                children: [
                  _Step(
                    number: 1,
                    text: 'Ask the other device to show their QR code.',
                    soulColor: soulColor,
                  ),
                  const SizedBox(height: 10),
                  _Step(
                    number: 2,
                    text: 'Tap "Start Scanning" and point your camera at it.',
                    soulColor: soulColor,
                  ),
                  const SizedBox(height: 10),
                  _Step(
                    number: 3,
                    text: 'Confirm the pairing to exchange PGP keys.',
                    soulColor: soulColor,
                  ),
                ],
              ),
            ),
            const SizedBox(height: 24),

            FilledButton.icon(
              onPressed: onStartScan,
              icon: const Icon(Icons.qr_code_scanner_rounded),
              label: const Text('Start Scanning'),
              style: FilledButton.styleFrom(
                backgroundColor: soulColor,
                foregroundColor: Colors.black,
                minimumSize: const Size(double.infinity, 52),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(14),
                ),
              ),
            ),
          ],
        ],
      ),
    );
  }
}

// ── Live scanner ───────────────────────────────────────────────────────────

class _LiveScanner extends StatefulWidget {
  const _LiveScanner({
    required this.onDetect,
    required this.onClose,
    required this.soulColor,
  });

  final void Function(BarcodeCapture) onDetect;
  final VoidCallback onClose;
  final Color soulColor;

  @override
  State<_LiveScanner> createState() => _LiveScannerState();
}

class _LiveScannerState extends State<_LiveScanner> {
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
    return Stack(
      children: [
        MobileScanner(
          controller: _controller,
          onDetect: widget.onDetect,
        ),
        // Corner frame overlay
        Positioned.fill(
          child: CustomPaint(
            painter: _ScanFramePainter(color: widget.soulColor, filled: false),
          ),
        ),
        // Close button
        Positioned(
          top: 16,
          right: 16,
          child: SafeArea(
            child: GestureDetector(
              onTap: widget.onClose,
              child: Container(
                decoration: const BoxDecoration(
                  color: Colors.black54,
                  shape: BoxShape.circle,
                ),
                padding: const EdgeInsets.all(8),
                child: const Icon(Icons.close, color: Colors.white, size: 22),
              ),
            ),
          ),
        ),
        // Bottom hint
        Positioned(
          bottom: 40,
          left: 0,
          right: 0,
          child: Center(
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 8),
              decoration: BoxDecoration(
                color: Colors.black54,
                borderRadius: BorderRadius.circular(20),
              ),
              child: const Text(
                'Align SKChat QR code within the frame',
                style: TextStyle(color: Colors.white, fontSize: 13),
              ),
            ),
          ),
        ),
      ],
    );
  }
}

// ── Scan result ────────────────────────────────────────────────────────────

class _ScanResult extends StatelessWidget {
  const _ScanResult({
    required this.value,
    required this.soulColor,
    required this.onConnect,
    required this.onReset,
  });

  final String value;
  final Color soulColor;
  final VoidCallback onConnect;
  final VoidCallback onReset;

  @override
  Widget build(BuildContext context) {
    final tt = Theme.of(context).textTheme;
    final isSkChat = value.startsWith('skchat://');
    final peerName = isSkChat ? _extractName(value) : null;

    return Column(
      children: [
        // Success icon
        Container(
          width: 72,
          height: 72,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: SovereignColors.accentEncrypt.withValues(alpha: 0.12),
          ),
          child: const Icon(
            Icons.check_circle_outline_rounded,
            size: 40,
            color: SovereignColors.accentEncrypt,
          ),
        ),
        const SizedBox(height: 16),
        Text(
          isSkChat ? 'Peer found!' : 'QR code scanned',
          style: tt.titleLarge?.copyWith(fontWeight: FontWeight.w700),
        ),
        const SizedBox(height: 8),
        if (peerName != null)
          Text(
            peerName,
            style: tt.titleMedium?.copyWith(color: soulColor),
          ),
        const SizedBox(height: 24),

        GlassCard(
          padding: const EdgeInsets.all(14),
          child: Row(
            children: [
              Icon(
                isSkChat ? Icons.person_rounded : Icons.qr_code_rounded,
                size: 18,
                color: SovereignColors.textSecondary,
              ),
              const SizedBox(width: 10),
              Expanded(
                child: Text(
                  value,
                  overflow: TextOverflow.ellipsis,
                  style: tt.bodySmall?.copyWith(
                    fontFamily: isSkChat ? 'JetBrainsMono' : null,
                    color: SovereignColors.textSecondary,
                    fontSize: 11,
                  ),
                ),
              ),
            ],
          ),
        ),
        const SizedBox(height: 24),

        if (isSkChat) ...[
          FilledButton.icon(
            onPressed: onConnect,
            icon: const Icon(Icons.link_rounded),
            label: const Text('Pair with this device'),
            style: FilledButton.styleFrom(
              backgroundColor: SovereignColors.accentEncrypt,
              foregroundColor: Colors.white,
              minimumSize: const Size(double.infinity, 52),
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(14),
              ),
            ),
          ),
          const SizedBox(height: 12),
        ],
        OutlinedButton.icon(
          onPressed: onReset,
          icon: const Icon(Icons.refresh_rounded, size: 16),
          label: const Text('Scan again'),
          style: OutlinedButton.styleFrom(
            foregroundColor: soulColor,
            side: BorderSide(color: soulColor.withValues(alpha: 0.5)),
            minimumSize: const Size(double.infinity, 48),
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(14),
            ),
          ),
        ),
      ],
    );
  }

  String _extractName(String uri) {
    try {
      return uri.replaceFirst('skchat://peer/', '').split('/').first;
    } catch (_) {
      return uri;
    }
  }
}

// ── Helpers ────────────────────────────────────────────────────────────────

class _Step extends StatelessWidget {
  const _Step({
    required this.number,
    required this.text,
    required this.soulColor,
  });

  final int number;
  final String text;
  final Color soulColor;

  @override
  Widget build(BuildContext context) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Container(
          width: 22,
          height: 22,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: soulColor.withValues(alpha: 0.15),
          ),
          child: Center(
            child: Text(
              '$number',
              style: TextStyle(
                fontSize: 11,
                fontWeight: FontWeight.w700,
                color: soulColor,
              ),
            ),
          ),
        ),
        const SizedBox(width: 10),
        Expanded(
          child: Text(
            text,
            style: const TextStyle(
              fontSize: 13,
              color: SovereignColors.textSecondary,
              height: 1.5,
            ),
          ),
        ),
      ],
    );
  }
}

/// Custom painter that draws a rounded-corner scan frame guide.
class _ScanFramePainter extends CustomPainter {
  _ScanFramePainter({required this.color, this.filled = true});

  final Color color;
  final bool filled;

  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()
      ..color = color.withValues(alpha: 0.6)
      ..strokeWidth = 3
      ..style = PaintingStyle.stroke;

    const cornerLen = 28.0;
    const margin = 48.0;
    const r = 12.0;

    final rect = Rect.fromLTWH(
      margin,
      (size.height - size.width + margin * 2) / 2,
      size.width - margin * 2,
      size.width - margin * 2,
    );

    // Draw 4 corner arcs
    final corners = [
      Offset(rect.left, rect.top),      // TL
      Offset(rect.right, rect.top),     // TR
      Offset(rect.right, rect.bottom),  // BR
      Offset(rect.left, rect.bottom),   // BL
    ];

    for (int i = 0; i < corners.length; i++) {
      final c = corners[i];
      final isRight = i == 1 || i == 2;
      final isBottom = i == 2 || i == 3;
      final dx = isRight ? -1.0 : 1.0;
      final dy = isBottom ? -1.0 : 1.0;

      final path = Path()
        ..moveTo(c.dx, c.dy + dy * cornerLen)
        ..lineTo(c.dx, c.dy + dy * r)
        ..arcToPoint(
          Offset(c.dx + dx * r, c.dy),
          radius: const Radius.circular(r),
          clockwise: isRight != isBottom,
        )
        ..lineTo(c.dx + dx * cornerLen, c.dy);
      canvas.drawPath(path, paint);
    }
  }

  @override
  bool shouldRepaint(_ScanFramePainter old) => old.color != color;
}
