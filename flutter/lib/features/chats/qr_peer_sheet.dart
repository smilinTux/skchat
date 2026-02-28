import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:mobile_scanner/mobile_scanner.dart';
import 'package:qr_flutter/qr_flutter.dart';
import '../../core/theme/theme.dart';
import '../../models/conversation.dart';
import '../../services/skcomm_client.dart';
import 'chats_provider.dart';

// ── Own-identity provider ──────────────────────────────────────────────────

class _PeerIdentity {
  const _PeerIdentity({required this.name, required this.fingerprint});

  final String name;
  final String fingerprint;

  String get qrData => jsonEncode({
        'type': 'skcomm-peer',
        'name': name,
        'fingerprint': fingerprint,
      });

  String get shortFingerprint {
    if (fingerprint.length < 16) return fingerprint;
    return '${fingerprint.substring(0, 8)}...${fingerprint.substring(fingerprint.length - 8)}';
  }
}

/// Fetches the local agent's identity from the daemon status endpoint.
final _selfIdentityProvider =
    FutureProvider.autoDispose<_PeerIdentity>((ref) async {
  final client = ref.read(skcommClientProvider);
  try {
    final status = await client.getStatus();
    return _PeerIdentity(
      name: status['agent'] as String? ?? 'unknown',
      fingerprint: status['fingerprint'] as String? ?? '',
    );
  } catch (_) {
    return const _PeerIdentity(name: 'unknown', fingerprint: '');
  }
});

// ── Sheet widget ───────────────────────────────────────────────────────────

/// Opens the QR add-peer sheet as a modal bottom sheet.
///
/// [onPeerAdded] is called with the new peerId after a successful scan.
/// The sheet dismisses itself before calling the callback.
Future<void> showQrPeerSheet(
  BuildContext context, {
  required void Function(String peerId) onPeerAdded,
}) {
  return showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    backgroundColor: Colors.transparent,
    builder: (_) => QrPeerSheet(onPeerAdded: onPeerAdded),
  );
}

/// QR add-peer bottom sheet with two tabs:
///   • My QR — shows your own QR code so peers can scan you
///   • Scan  — scans a peer's QR code to add them
class QrPeerSheet extends ConsumerStatefulWidget {
  const QrPeerSheet({super.key, required this.onPeerAdded});

  final void Function(String peerId) onPeerAdded;

  @override
  ConsumerState<QrPeerSheet> createState() => _QrPeerSheetState();
}

class _QrPeerSheetState extends ConsumerState<QrPeerSheet>
    with SingleTickerProviderStateMixin {
  late TabController _tabController;
  MobileScannerController? _scanner;
  bool _scanned = false;
  String? _scanError;

  @override
  void initState() {
    super.initState();
    _tabController = TabController(length: 2, vsync: this);
    _tabController.addListener(_onTabChanged);
  }

  void _onTabChanged() {
    if (!_tabController.indexIsChanging) return;
    setState(() {});
    if (_tabController.index == 1) {
      // Entering scan tab — initialise scanner lazily.
      _scanner ??= MobileScannerController();
    } else {
      // Leaving scan tab — pause camera to save resources.
      _scanner?.stop();
    }
  }

  @override
  void dispose() {
    _tabController.removeListener(_onTabChanged);
    _tabController.dispose();
    _scanner?.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final tt = Theme.of(context).textTheme;
    final bottomPad = MediaQuery.of(context).padding.bottom;

    return ClipRRect(
      borderRadius: const BorderRadius.vertical(top: Radius.circular(24)),
      child: Container(
        color: SovereignColors.surfaceRaised,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            // Drag handle
            Padding(
              padding: const EdgeInsets.only(top: 12, bottom: 4),
              child: Container(
                width: 40,
                height: 4,
                decoration: BoxDecoration(
                  color: SovereignColors.textTertiary,
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
            ),

            // Header row
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 4),
              child: Row(
                children: [
                  const SizedBox(width: 12),
                  Text(
                    'Add Peer',
                    style: tt.titleLarge?.copyWith(
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                  const Spacer(),
                  IconButton(
                    icon: const Icon(Icons.close_rounded),
                    color: SovereignColors.textSecondary,
                    onPressed: () => Navigator.of(context).pop(),
                  ),
                ],
              ),
            ),

            // Tab bar
            TabBar(
              controller: _tabController,
              labelColor: SovereignColors.soulLumina,
              unselectedLabelColor: SovereignColors.textSecondary,
              indicatorColor: SovereignColors.soulLumina,
              dividerColor: SovereignColors.surfaceGlassBorder,
              tabs: const [
                Tab(
                  icon: Icon(Icons.qr_code_rounded),
                  text: 'My QR',
                ),
                Tab(
                  icon: Icon(Icons.qr_code_scanner_rounded),
                  text: 'Scan',
                ),
              ],
            ),

            // Fixed-height tab content
            SizedBox(
              height: 340,
              child: TabBarView(
                controller: _tabController,
                children: [
                  _MyQrTab(tt: tt),
                  _buildScanTab(tt),
                ],
              ),
            ),

            SizedBox(height: bottomPad + 16),
          ],
        ),
      ),
    );
  }

  Widget _buildScanTab(TextTheme tt) {
    if (_scanned) {
      return Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          const Icon(
            Icons.check_circle_rounded,
            size: 64,
            color: SovereignColors.accentEncrypt,
          ),
          const SizedBox(height: 12),
          Text(
            'Peer added!',
            style: tt.titleMedium?.copyWith(
              color: SovereignColors.accentEncrypt,
            ),
          ),
          const SizedBox(height: 4),
          Text(
            'Opening conversation…',
            style: tt.bodySmall?.copyWith(
              color: SovereignColors.textSecondary,
            ),
          ),
        ],
      );
    }

    // Only build the MobileScanner widget when on the scan tab.
    final isScanActive = _tabController.index == 1;

    return Column(
      children: [
        if (_scanError != null)
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
            child: Row(
              children: [
                const Icon(
                  Icons.warning_amber_rounded,
                  size: 14,
                  color: SovereignColors.accentDanger,
                ),
                const SizedBox(width: 6),
                Expanded(
                  child: Text(
                    _scanError!,
                    style: tt.bodySmall?.copyWith(
                      color: SovereignColors.accentDanger,
                    ),
                  ),
                ),
              ],
            ),
          ),
        Expanded(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: ClipRRect(
              borderRadius: BorderRadius.circular(16),
              child: isScanActive
                  ? MobileScanner(
                      controller: _scanner ??= MobileScannerController(),
                      onDetect: _onDetect,
                    )
                  : Container(color: SovereignColors.surfaceBase),
            ),
          ),
        ),
        Padding(
          padding: const EdgeInsets.only(bottom: 12),
          child: Text(
            'Point at a peer\'s SKChat QR code',
            style: tt.bodySmall?.copyWith(
              color: SovereignColors.textSecondary,
            ),
          ),
        ),
      ],
    );
  }

  void _onDetect(BarcodeCapture capture) {
    if (_scanned) return;

    for (final barcode in capture.barcodes) {
      final raw = barcode.rawValue;
      if (raw == null || raw.isEmpty) continue;

      try {
        final data = jsonDecode(raw);
        if (data is! Map<String, dynamic>) continue;
        if (data['type'] != 'skcomm-peer') {
          setState(() => _scanError = 'Not a SKChat QR code.');
          continue;
        }

        final name = (data['name'] as String? ?? '').trim();
        final fingerprint = data['fingerprint'] as String? ?? '';
        if (name.isEmpty) {
          setState(() => _scanError = 'QR code is missing peer name.');
          continue;
        }

        // Mark scanned to prevent duplicate handling.
        setState(() {
          _scanned = true;
          _scanError = null;
        });
        _scanner?.stop();

        final lowerName = name.toLowerCase();
        final conversation = Conversation(
          peerId: lowerName,
          displayName: name,
          lastMessage: '',
          lastMessageTime: DateTime.now(),
          soulColor: null,
          soulFingerprint: fingerprint.isNotEmpty ? fingerprint : lowerName,
          isOnline: false,
          isAgent: const {'lumina', 'jarvis', 'opus', 'ava', 'ara'}
              .contains(lowerName),
          lastDeliveryStatus: 'sent',
        );

        ref.read(chatsProvider.notifier).addConversation(conversation);

        // Brief success display, then close sheet and navigate.
        Future.delayed(const Duration(milliseconds: 700), () {
          if (mounted) {
            Navigator.of(context).pop();
            widget.onPeerAdded(lowerName);
          }
        });
        return;
      } catch (_) {
        setState(() => _scanError = 'Invalid QR code.');
      }
    }
  }
}

// ── My QR tab ──────────────────────────────────────────────────────────────

/// Separate StatelessWidget so it can watch the identity provider independently.
class _MyQrTab extends ConsumerWidget {
  const _MyQrTab({required this.tt});

  final TextTheme tt;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final identityAsync = ref.watch(_selfIdentityProvider);

    return identityAsync.when(
      loading: () => const Center(
        child: CircularProgressIndicator(color: SovereignColors.soulLumina),
      ),
      error: (_, __) => Center(
        child: Text(
          'Could not load identity.\nIs the daemon running?',
          style: tt.bodyMedium?.copyWith(
            color: SovereignColors.textSecondary,
          ),
          textAlign: TextAlign.center,
        ),
      ),
      data: (identity) => _buildQrContent(context, identity),
    );
  }

  Widget _buildQrContent(BuildContext context, _PeerIdentity identity) {
    return SingleChildScrollView(
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 16),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            // QR code — white background required for scanner contrast
            Container(
              decoration: BoxDecoration(
                color: Colors.white,
                borderRadius: BorderRadius.circular(16),
              ),
              padding: const EdgeInsets.all(16),
              child: QrImageView(
                data: identity.qrData,
                version: QrVersions.auto,
                size: 180,
                eyeStyle: const QrEyeStyle(
                  eyeShape: QrEyeShape.square,
                  color: Colors.black,
                ),
                dataModuleStyle: const QrDataModuleStyle(
                  dataModuleShape: QrDataModuleShape.square,
                  color: Colors.black,
                ),
              ),
            ),

            const SizedBox(height: 16),

            // Agent name
            Text(
              identity.name,
              style: tt.titleMedium?.copyWith(fontWeight: FontWeight.w700),
            ),

            // Short fingerprint
            if (identity.fingerprint.isNotEmpty) ...[
              const SizedBox(height: 4),
              GestureDetector(
                onTap: () => _copyIdentity(context, identity),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(
                      identity.shortFingerprint,
                      style: tt.bodySmall?.copyWith(
                        color: SovereignColors.textSecondary,
                        fontFamily: 'monospace',
                      ),
                    ),
                    const SizedBox(width: 4),
                    const Icon(
                      Icons.copy_rounded,
                      size: 12,
                      color: SovereignColors.textTertiary,
                    ),
                  ],
                ),
              ),
            ],

            const SizedBox(height: 16),

            // Copy identity button
            OutlinedButton.icon(
              onPressed: () => _copyIdentity(context, identity),
              icon: const Icon(Icons.copy_rounded, size: 14),
              label: const Text('Copy identity'),
              style: OutlinedButton.styleFrom(
                foregroundColor: SovereignColors.textSecondary,
                side: const BorderSide(
                  color: SovereignColors.surfaceGlassBorder,
                ),
                visualDensity: VisualDensity.compact,
              ),
            ),
          ],
        ),
      ),
    );
  }

  Future<void> _copyIdentity(
    BuildContext context,
    _PeerIdentity identity,
  ) async {
    await Clipboard.setData(ClipboardData(text: identity.qrData));
    if (context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: const Text('Identity copied to clipboard'),
          backgroundColor: SovereignColors.surfaceRaised,
          behavior: SnackBarBehavior.floating,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(12),
          ),
          duration: const Duration(seconds: 2),
        ),
      );
    }
  }
}
