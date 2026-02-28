import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import '../../core/theme/theme.dart';
import '../../core/providers/theme_provider.dart';
import '../../services/skcomm_sync.dart';

// ── Local identity provider ────────────────────────────────────────────────
// In the real app this is populated from the CapAuth keychain.
// Exposed as a Notifier so it can be updated at runtime.

class LocalIdentity {
  const LocalIdentity({
    this.displayName = 'You',
    this.fingerprint = '',
    this.pgpKeyId = '',
    this.pgpKeySize = 4096,
    this.daemonUrl = 'localhost:9384',
  });

  final String displayName;
  final String fingerprint;
  final String pgpKeyId;
  final int pgpKeySize;
  final String daemonUrl;

  LocalIdentity copyWith({
    String? displayName,
    String? fingerprint,
    String? pgpKeyId,
    int? pgpKeySize,
    String? daemonUrl,
  }) {
    return LocalIdentity(
      displayName: displayName ?? this.displayName,
      fingerprint: fingerprint ?? this.fingerprint,
      pgpKeyId: pgpKeyId ?? this.pgpKeyId,
      pgpKeySize: pgpKeySize ?? this.pgpKeySize,
      daemonUrl: daemonUrl ?? this.daemonUrl,
    );
  }
}

class LocalIdentityNotifier extends Notifier<LocalIdentity> {
  @override
  LocalIdentity build() => const LocalIdentity(
        displayName: 'Sovereign Node',
        fingerprint: 'CCBE9306410CF8CD5E393D6DEC31663B95230684',
        pgpKeyId: '95230684',
        pgpKeySize: 4096,
        daemonUrl: 'localhost:9384',
      );

  void update(LocalIdentity identity) => state = identity;
}

final localIdentityProvider =
    NotifierProvider<LocalIdentityNotifier, LocalIdentity>(
  LocalIdentityNotifier.new,
);

// ── Transport health provider ─────────────────────────────────────────────

final transportHealthProvider =
    Provider<List<({String name, bool active})>>((ref) {
  final daemon = ref.watch(skcommSyncProvider);
  final info = daemon.transportInfo;
  if (info == null) return [];

  final transports = <({String name, bool active})>[];
  final raw = info['transports'];
  if (raw is Map) {
    for (final entry in raw.entries) {
      final active = (entry.value as Map?)?['available'] as bool? ?? false;
      transports.add((name: entry.key as String, active: active));
    }
  } else if (raw is List) {
    for (final t in raw) {
      if (t is Map) {
        transports.add((
          name: t['transport'] as String? ?? 'unknown',
          active: t['available'] as bool? ?? false,
        ));
      }
    }
  }
  if (transports.isEmpty && daemon.status == DaemonStatus.online) {
    transports.add((name: 'file', active: true));
  }
  return transports;
});

// ── Profile screen ─────────────────────────────────────────────────────────

/// Me / Profile / Settings screen.
/// Identity card with soul color + fingerprint, daemon health,
/// transport status, appearance settings, encryption keys, QR code login.
class ProfileScreen extends ConsumerWidget {
  const ProfileScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final identity = ref.watch(localIdentityProvider);
    final daemon = ref.watch(skcommSyncProvider);
    final transports = ref.watch(transportHealthProvider);
    final themeMode = ref.watch(themeProvider);
    final soulColor = SovereignColors.fromFingerprint(identity.fingerprint);
    final tt = Theme.of(context).textTheme;

    return Scaffold(
      backgroundColor: SovereignColors.surfaceBase,
      appBar: AppBar(
        backgroundColor: SovereignColors.surfaceBase,
        title: Text('Me', style: tt.displayLarge?.copyWith(fontSize: 24)),
        actions: [
          IconButton(
            icon: const Icon(Icons.qr_code_rounded),
            tooltip: 'QR Login',
            onPressed: () => context.push('/login/qr'),
          ),
          const SizedBox(width: 4),
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.only(bottom: 120),
        children: [
          // ── Identity card ────────────────────────────────────────────
          _IdentityHeader(identity: identity, soulColor: soulColor),
          const SizedBox(height: 20),

          // ── Daemon & network health ──────────────────────────────────
          _SectionLabel(label: 'Network'),
          _DaemonStatusCard(daemon: daemon, transports: transports),
          const SizedBox(height: 20),

          // ── Encryption ──────────────────────────────────────────────
          _SectionLabel(label: 'Encryption'),
          _EncryptionCard(identity: identity),
          const SizedBox(height: 20),

          // ── Appearance ──────────────────────────────────────────────
          _SectionLabel(label: 'Appearance'),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16),
            child: GlassCard(
              padding: EdgeInsets.zero,
              child: Column(
                children: [
                  ListTile(
                    leading: const Icon(Icons.dark_mode_rounded),
                    title: const Text('Dark mode'),
                    trailing: Switch(
                      value: themeMode == ThemeMode.dark,
                      onChanged: (v) {
                        if (v) {
                          ref.read(themeProvider.notifier).setDark();
                        } else {
                          ref.read(themeProvider.notifier).setLight();
                        }
                      },
                      activeColor: soulColor,
                    ),
                  ),
                  const Divider(height: 1, indent: 56),
                  ListTile(
                    leading: const Icon(Icons.palette_outlined),
                    title: const Text('Soul color'),
                    subtitle: Text(
                      'Derived from fingerprint',
                      style: tt.labelSmall?.copyWith(
                        color: SovereignColors.textTertiary,
                      ),
                    ),
                    trailing: Container(
                      width: 24,
                      height: 24,
                      decoration: BoxDecoration(
                        color: soulColor,
                        shape: BoxShape.circle,
                        boxShadow: [
                          BoxShadow(
                            color: soulColor.withValues(alpha: 0.4),
                            blurRadius: 8,
                          ),
                        ],
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 20),

          // ── Quick actions ────────────────────────────────────────────
          _SectionLabel(label: 'Quick actions'),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16),
            child: GlassCard(
              padding: EdgeInsets.zero,
              child: Column(
                children: [
                  ListTile(
                    leading: const Icon(Icons.qr_code_scanner_rounded),
                    title: const Text('QR Login / Pair Device'),
                    subtitle: const Text('Show or scan a QR code'),
                    trailing: const Icon(Icons.chevron_right_rounded),
                    onTap: () => context.push('/login/qr'),
                  ),
                  const Divider(height: 1, indent: 56),
                  ListTile(
                    leading: const Icon(Icons.storage_outlined),
                    title: const Text('SKComm Daemon'),
                    subtitle: Text(
                      identity.daemonUrl,
                      style: tt.labelSmall?.copyWith(
                        color: SovereignColors.textTertiary,
                        fontFamily: 'JetBrainsMono',
                      ),
                    ),
                    trailing: const Icon(Icons.chevron_right_rounded),
                    onTap: () => _showDaemonSettings(context, ref, identity),
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 20),

          // ── About ───────────────────────────────────────────────────
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16),
            child: GlassCard(
              padding: EdgeInsets.zero,
              child: ListTile(
                leading: const Icon(Icons.info_outline_rounded),
                title: const Text('About SKChat'),
                subtitle: const Text('Sovereign P2P messaging'),
                trailing: const Icon(Icons.chevron_right_rounded),
                onTap: () {},
              ),
            ),
          ),
        ],
      ),
    );
  }

  void _showDaemonSettings(
    BuildContext context,
    WidgetRef ref,
    LocalIdentity identity,
  ) {
    final controller = TextEditingController(text: identity.daemonUrl);
    showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      backgroundColor: SovereignColors.surfaceRaised,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      builder: (ctx) => Padding(
        padding: EdgeInsets.only(
          left: 24,
          right: 24,
          top: 24,
          bottom: MediaQuery.of(ctx).viewInsets.bottom + 24,
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'SKComm Daemon URL',
              style: Theme.of(context).textTheme.titleMedium?.copyWith(
                    fontWeight: FontWeight.w700,
                  ),
            ),
            const SizedBox(height: 16),
            TextField(
              controller: controller,
              style: const TextStyle(
                fontFamily: 'JetBrainsMono',
                fontSize: 14,
                color: SovereignColors.textPrimary,
              ),
              decoration: InputDecoration(
                hintText: 'localhost:9384',
                hintStyle:
                    const TextStyle(color: SovereignColors.textTertiary),
                enabledBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(12),
                  borderSide: const BorderSide(
                    color: SovereignColors.surfaceGlassBorder,
                  ),
                ),
                focusedBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(12),
                  borderSide: const BorderSide(
                    color: SovereignColors.soulLumina,
                  ),
                ),
                filled: true,
                fillColor: SovereignColors.surfaceGlass,
              ),
            ),
            const SizedBox(height: 16),
            FilledButton(
              onPressed: () {
                ref.read(localIdentityProvider.notifier).update(
                      identity.copyWith(
                        daemonUrl: controller.text.trim(),
                      ),
                    );
                Navigator.of(ctx).pop();
              },
              style: FilledButton.styleFrom(
                backgroundColor: SovereignColors.soulLumina,
                foregroundColor: Colors.black,
                minimumSize: const Size(double.infinity, 48),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12),
                ),
              ),
              child: const Text('Save'),
            ),
          ],
        ),
      ),
    );
  }
}

// ── Identity header ────────────────────────────────────────────────────────

class _IdentityHeader extends StatelessWidget {
  const _IdentityHeader({required this.identity, required this.soulColor});

  final LocalIdentity identity;
  final Color soulColor;

  @override
  Widget build(BuildContext context) {
    final tt = Theme.of(context).textTheme;

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16),
      child: GlassCard(
        child: Column(
          children: [
            // Soul-color gradient bar
            Container(
              height: 4,
              margin: const EdgeInsets.only(bottom: 16),
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(2),
                gradient: LinearGradient(
                  colors: [
                    soulColor.withValues(alpha: 0.8),
                    soulColor.withValues(alpha: 0.2),
                  ],
                ),
              ),
            ),
            Row(
              children: [
                SoulAvatar(
                  soulColor: soulColor,
                  initials: identity.displayName.isNotEmpty
                      ? identity.displayName[0].toUpperCase()
                      : 'S',
                  isOnline: true,
                  size: 64,
                  ringWidth: 3,
                ),
                const SizedBox(width: 16),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        identity.displayName,
                        style: tt.titleLarge?.copyWith(
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                      const SizedBox(height: 4),
                      Row(
                        children: [
                          const EncryptBadge(size: 12),
                          const SizedBox(width: 4),
                          Text(
                            'CapAuth Identity',
                            style: tt.labelSmall?.copyWith(
                              color: SovereignColors.accentEncrypt,
                            ),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              ],
            ),
            if (identity.fingerprint.isNotEmpty) ...[
              const SizedBox(height: 16),
              const Divider(
                height: 1,
                color: SovereignColors.surfaceGlassBorder,
              ),
              const SizedBox(height: 12),
              Row(
                children: [
                  Icon(
                    Icons.fingerprint_rounded,
                    size: 14,
                    color: SovereignColors.textTertiary,
                  ),
                  const SizedBox(width: 6),
                  Text(
                    'Fingerprint',
                    style: tt.labelSmall?.copyWith(
                      color: SovereignColors.textTertiary,
                    ),
                  ),
                  const Spacer(),
                  GestureDetector(
                    onTap: () {
                      Clipboard.setData(
                        ClipboardData(text: identity.fingerprint),
                      );
                      ScaffoldMessenger.of(context).showSnackBar(
                        const SnackBar(
                          content: Text('Fingerprint copied'),
                          duration: Duration(seconds: 1),
                        ),
                      );
                    },
                    child: const Icon(
                      Icons.copy_rounded,
                      size: 14,
                      color: SovereignColors.textTertiary,
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 6),
              Text(
                _formatFingerprint(identity.fingerprint),
                style: tt.labelSmall?.copyWith(
                  fontFamily: 'JetBrainsMono',
                  color: soulColor.withValues(alpha: 0.9),
                  fontSize: 11,
                  letterSpacing: 1.0,
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }

  String _formatFingerprint(String fp) {
    if (fp.length < 8) return fp;
    final clean = fp.replaceAll(' ', '').toUpperCase();
    final groups = <String>[];
    for (int i = 0; i < clean.length; i += 4) {
      groups.add(clean.substring(i, (i + 4).clamp(0, clean.length)));
    }
    return groups.join(' ');
  }
}

// ── Daemon status card ─────────────────────────────────────────────────────

class _DaemonStatusCard extends StatelessWidget {
  const _DaemonStatusCard({
    required this.daemon,
    required this.transports,
  });

  final DaemonState daemon;
  final List<({String name, bool active})> transports;

  @override
  Widget build(BuildContext context) {
    final tt = Theme.of(context).textTheme;
    final (statusLabel, statusColor, statusIcon) = switch (daemon.status) {
      DaemonStatus.online => (
          'Online',
          SovereignColors.accentEncrypt,
          Icons.circle,
        ),
      DaemonStatus.offline => (
          'Offline',
          SovereignColors.accentDanger,
          Icons.circle,
        ),
      DaemonStatus.error => (
          'Error',
          SovereignColors.accentWarning,
          Icons.warning_rounded,
        ),
      DaemonStatus.connecting => (
          'Connecting',
          SovereignColors.textTertiary,
          Icons.circle,
        ),
    };

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16),
      child: GlassCard(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(statusIcon, size: 10, color: statusColor),
                const SizedBox(width: 8),
                Text(
                  'SKComm Daemon · $statusLabel',
                  style: tt.titleSmall?.copyWith(
                    color: statusColor,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const Spacer(),
                if (daemon.lastPollAt != null)
                  Text(
                    _lastPollText(daemon.lastPollAt!),
                    style: tt.labelSmall?.copyWith(
                      color: SovereignColors.textTertiary,
                    ),
                  ),
              ],
            ),
            if (daemon.errorMessage != null) ...[
              const SizedBox(height: 8),
              Text(
                daemon.errorMessage!,
                style: tt.bodySmall?.copyWith(
                  color: SovereignColors.accentWarning,
                ),
              ),
            ],
            if (transports.isNotEmpty) ...[
              const SizedBox(height: 12),
              Wrap(
                spacing: 8,
                runSpacing: 6,
                children: transports
                    .map((t) => _TransportChip(name: t.name, active: t.active))
                    .toList(),
              ),
            ] else if (daemon.status == DaemonStatus.online) ...[
              const SizedBox(height: 8),
              Text(
                'Encrypted P2P transport active',
                style: tt.bodySmall?.copyWith(
                  color: SovereignColors.textSecondary,
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }

  String _lastPollText(DateTime t) {
    final diff = DateTime.now().difference(t);
    if (diff.inSeconds < 10) return 'just now';
    if (diff.inMinutes < 1) return '${diff.inSeconds}s ago';
    return '${diff.inMinutes}m ago';
  }
}

class _TransportChip extends StatelessWidget {
  const _TransportChip({required this.name, required this.active});

  final String name;
  final bool active;

  @override
  Widget build(BuildContext context) {
    final color =
        active ? SovereignColors.accentEncrypt : SovereignColors.textTertiary;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.1),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: color.withValues(alpha: 0.25)),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(
            active
                ? Icons.check_circle_outline
                : Icons.radio_button_unchecked,
            size: 12,
            color: color,
          ),
          const SizedBox(width: 4),
          Text(
            name,
            style: TextStyle(
              fontSize: 11,
              color: color,
              fontWeight: FontWeight.w500,
            ),
          ),
        ],
      ),
    );
  }
}

// ── Encryption card ────────────────────────────────────────────────────────

class _EncryptionCard extends StatelessWidget {
  const _EncryptionCard({required this.identity});

  final LocalIdentity identity;

  @override
  Widget build(BuildContext context) {
    final tt = Theme.of(context).textTheme;

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16),
      child: GlassCard(
        padding: EdgeInsets.zero,
        child: Column(
          children: [
            ListTile(
              leading: const Icon(
                Icons.key_rounded,
                color: SovereignColors.accentEncrypt,
              ),
              title: const Text('PGP Key'),
              subtitle: Text(
                'RSA ${identity.pgpKeySize}-bit · ID: ${identity.pgpKeyId}',
                style: tt.labelSmall?.copyWith(
                  fontFamily: 'JetBrainsMono',
                  color: SovereignColors.textTertiary,
                  fontSize: 11,
                ),
              ),
              trailing: Container(
                padding:
                    const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                decoration: BoxDecoration(
                  color:
                      SovereignColors.accentEncrypt.withValues(alpha: 0.12),
                  borderRadius: BorderRadius.circular(8),
                ),
                child: Text(
                  'Active',
                  style: tt.labelSmall?.copyWith(
                    color: SovereignColors.accentEncrypt,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
            ),
            const Divider(height: 1, indent: 56),
            ListTile(
              leading: const Icon(Icons.verified_user_outlined),
              title: const Text('Trust Level'),
              subtitle: const Text('Full trust · Self-sovereign'),
              trailing: const Icon(Icons.chevron_right_rounded),
              onTap: () {},
            ),
            const Divider(height: 1, indent: 56),
            ListTile(
              leading: const Icon(Icons.rotate_right_rounded),
              title: const Text('Rotate Keys'),
              subtitle: const Text('Generate new PGP keypair'),
              trailing: const Icon(Icons.chevron_right_rounded),
              onTap: () {
                ScaffoldMessenger.of(context).showSnackBar(
                  const SnackBar(
                    content: Text('Key rotation — coming soon'),
                  ),
                );
              },
            ),
          ],
        ),
      ),
    );
  }
}

// ── Section label ──────────────────────────────────────────────────────────

class _SectionLabel extends StatelessWidget {
  const _SectionLabel({required this.label});

  final String label;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 0, 20, 8),
      child: Text(
        label.toUpperCase(),
        style: Theme.of(context).textTheme.labelSmall?.copyWith(
              color: SovereignColors.textTertiary,
              letterSpacing: 1.2,
              fontWeight: FontWeight.w600,
            ),
      ),
    );
  }
}
