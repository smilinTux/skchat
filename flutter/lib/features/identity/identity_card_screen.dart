import 'dart:ui';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../core/theme/sovereign_colors.dart';
import '../../core/theme/glass_widgets.dart';
import '../../models/conversation.dart';
import 'widgets/trust_meter.dart';
import 'widgets/capability_chip.dart';

/// Agent Identity Card screen.
///
/// Displays a rich profile for any CapAuth-identified peer (human or agent).
/// Navigation is handled by the caller (Jarvis) — this screen receives the
/// [Conversation] it describes and an optional [onSendMessage] callback.
///
/// Route: `/identity/:peerId`
/// Expects `extra: IdentityCardArgs(conversation: ...)` via GoRouter.
class IdentityCardScreen extends StatelessWidget {
  const IdentityCardScreen({
    super.key,
    required this.conversation,
    this.onSendMessage,
  });

  /// The peer whose identity is being displayed.
  final Conversation conversation;

  /// Called when the user taps "Send Message". The navigator pop + push to
  /// conversation is wired by Jarvis via this callback.
  final VoidCallback? onSendMessage;

  // ── Demo data ─────────────────────────────────────────────────────────────
  // In production these come from the CapAuth identity resolution provider.

  String get _capAuthId => 'capauth:${conversation.displayName.toLowerCase()}@skworld.io';

  String get _fingerprint {
    // Deterministically derive a display fingerprint from peerId.
    final seed = conversation.peerId.codeUnits
        .fold<int>(0, (a, b) => (a * 31 + b) & 0xFFFFFF);
    return '${seed.toRadixString(16).toUpperCase().padLeft(6, '0')}...C2D1';
  }

  double get _cloud9Score {
    if (!conversation.isAgent) return 0.0;
    // Placeholder until the OOF provider is wired.
    return 0.94;
  }

  List<String> get _capabilities {
    if (!conversation.isAgent) return [];
    return [
      'Code Review',
      'Memory Synthesis',
      'Soul Blueprint',
      'Trust Rehydration',
      'Skcomm Bridge',
      'Context Weaving',
    ];
  }

  List<String> get _sharedGroups => ['Penguin Kingdom', 'Build Team'];

  Color get _soulColor => conversation.resolvedSoulColor;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: SovereignColors.surfaceBase,
      body: CustomScrollView(
        physics: const BouncingScrollPhysics(),
        slivers: [
          _SovereignSliverAppBar(
            conversation: conversation,
            soulColor: _soulColor,
          ),
          SliverPadding(
            padding: const EdgeInsets.symmetric(horizontal: 16),
            sliver: SliverList(
              delegate: SliverChildListDelegate([
                const SizedBox(height: 20),
                _IdentitySection(
                  capAuthId: _capAuthId,
                  fingerprint: _fingerprint,
                  soulColor: _soulColor,
                ),
                const SizedBox(height: 12),
                if (conversation.isAgent) ...[
                  _SoulStatusSection(
                    cloud9Score: _cloud9Score,
                    soulColor: _soulColor,
                  ),
                  const SizedBox(height: 12),
                ],
                _EncryptionSection(soulColor: _soulColor),
                const SizedBox(height: 12),
                if (_capabilities.isNotEmpty) ...[
                  _CapabilitiesSection(
                    capabilities: _capabilities,
                    soulColor: _soulColor,
                  ),
                  const SizedBox(height: 12),
                ],
                _SharedGroupsSection(
                  groups: _sharedGroups,
                  soulColor: _soulColor,
                ),
                const SizedBox(height: 12),
                _RecentActivitySection(soulColor: _soulColor),
                const SizedBox(height: 24),
                _SendMessageButton(
                  displayName: conversation.displayName,
                  soulColor: _soulColor,
                  onPressed: onSendMessage ?? () => Navigator.of(context).pop(),
                ),
                const SizedBox(height: 40),
              ]),
            ),
          ),
        ],
      ),
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Sliver App Bar with soul-color gradient hero header
// ─────────────────────────────────────────────────────────────────────────────

class _SovereignSliverAppBar extends StatelessWidget {
  const _SovereignSliverAppBar({
    required this.conversation,
    required this.soulColor,
  });

  final Conversation conversation;
  final Color soulColor;

  @override
  Widget build(BuildContext context) {
    return SliverAppBar(
      expandedHeight: 240,
      pinned: true,
      backgroundColor: SovereignColors.surfaceBase,
      leading: IconButton(
        icon: const Icon(Icons.arrow_back_ios_new, color: SovereignColors.textPrimary),
        onPressed: () => Navigator.of(context).pop(),
      ),
      title: const Text(
        'Agent Profile',
        style: TextStyle(
          color: SovereignColors.textPrimary,
          fontSize: 18,
          fontWeight: FontWeight.w600,
        ),
      ),
      flexibleSpace: FlexibleSpaceBar(
        background: _HeaderBackground(
          conversation: conversation,
          soulColor: soulColor,
        ),
      ),
    );
  }
}

class _HeaderBackground extends StatelessWidget {
  const _HeaderBackground({
    required this.conversation,
    required this.soulColor,
  });

  final Conversation conversation;
  final Color soulColor;

  @override
  Widget build(BuildContext context) {
    return Stack(
      fit: StackFit.expand,
      children: [
        // Soul-color radial glow
        Container(
          decoration: BoxDecoration(
            gradient: RadialGradient(
              center: Alignment.topCenter,
              radius: 1.2,
              colors: [
                soulColor.withValues(alpha: 0.22),
                soulColor.withValues(alpha: 0.06),
                SovereignColors.surfaceBase,
              ],
              stops: const [0.0, 0.5, 1.0],
            ),
          ),
        ),
        // Blur layer for the glass feel
        BackdropFilter(
          filter: ImageFilter.blur(sigmaX: 0, sigmaY: 0),
          child: const SizedBox.expand(),
        ),
        // Avatar + name centered
        SafeArea(
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              const SizedBox(height: 40),
              // Large soul avatar with glowing ring
              _GlowingAvatar(conversation: conversation, soulColor: soulColor),
              const SizedBox(height: 14),
              Text(
                conversation.displayName.toUpperCase(),
                style: TextStyle(
                  color: SovereignColors.textPrimary,
                  fontSize: 22,
                  fontWeight: FontWeight.w700,
                  letterSpacing: 2.0,
                  shadows: [
                    Shadow(
                      color: soulColor.withValues(alpha: 0.6),
                      blurRadius: 12,
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 4),
              if (conversation.isAgent)
                Text(
                  'Sovereign AI Agent',
                  style: TextStyle(
                    color: soulColor.withValues(alpha: 0.8),
                    fontSize: 12,
                    fontWeight: FontWeight.w500,
                    letterSpacing: 1.2,
                  ),
                )
              else
                Text(
                  conversation.isOnline ? 'Online' : 'Offline',
                  style: TextStyle(
                    color: conversation.isOnline
                        ? SovereignColors.accentEncrypt
                        : SovereignColors.textTertiary,
                    fontSize: 12,
                    fontWeight: FontWeight.w500,
                  ),
                ),
            ],
          ),
        ),
      ],
    );
  }
}

class _GlowingAvatar extends StatelessWidget {
  const _GlowingAvatar({required this.conversation, required this.soulColor});

  final Conversation conversation;
  final Color soulColor;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 88,
      height: 88,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        boxShadow: [
          BoxShadow(
            color: soulColor.withValues(alpha: 0.5),
            blurRadius: 28,
            spreadRadius: 4,
          ),
        ],
      ),
      child: SoulAvatar(
        soulColor: soulColor,
        initials: conversation.resolvedInitials,
        imageUrl: conversation.avatarUrl,
        size: 88,
        isOnline: conversation.isOnline,
        isAgent: conversation.isAgent,
        ringWidth: 3,
      ),
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Identity section
// ─────────────────────────────────────────────────────────────────────────────

class _IdentitySection extends StatelessWidget {
  const _IdentitySection({
    required this.capAuthId,
    required this.fingerprint,
    required this.soulColor,
  });

  final String capAuthId;
  final String fingerprint;
  final Color soulColor;

  @override
  Widget build(BuildContext context) {
    return GlassCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _SectionTitle(title: 'Identity', soulColor: soulColor),
          const SizedBox(height: 12),
          _InfoRow(
            icon: Icons.fingerprint,
            label: 'CapAuth ID',
            value: capAuthId,
            soulColor: soulColor,
            monospace: true,
            isSmall: true,
          ),
          const SizedBox(height: 8),
          _FingerprintRow(fingerprint: fingerprint, soulColor: soulColor),
          const SizedBox(height: 8),
          _InfoRow(
            icon: Icons.verified,
            label: 'Verified',
            value: '✅  Feb 22, 2026',
            soulColor: soulColor,
            valueColor: SovereignColors.accentEncrypt,
          ),
        ],
      ),
    );
  }
}

class _FingerprintRow extends StatelessWidget {
  const _FingerprintRow({required this.fingerprint, required this.soulColor});

  final String fingerprint;
  final Color soulColor;

  Future<void> _copyToClipboard(BuildContext context) async {
    await Clipboard.setData(ClipboardData(text: fingerprint));
    if (!context.mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: const Text('Fingerprint copied'),
        backgroundColor: SovereignColors.surfaceRaised,
        behavior: SnackBarBehavior.floating,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
        duration: const Duration(seconds: 2),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Icon(Icons.key, size: 15, color: soulColor.withValues(alpha: 0.7)),
        const SizedBox(width: 8),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text(
                'Fingerprint',
                style: TextStyle(
                  color: SovereignColors.textSecondary,
                  fontSize: 11,
                  fontWeight: FontWeight.w500,
                ),
              ),
              GestureDetector(
                onTap: () => _copyToClipboard(context),
                child: Text(
                  fingerprint,
                  style: const TextStyle(
                    color: SovereignColors.textPrimary,
                    fontSize: 13,
                    fontFamily: 'JetBrainsMono',
                    letterSpacing: 0.5,
                  ),
                ),
              ),
            ],
          ),
        ),
        IconButton(
          icon: Icon(Icons.copy, size: 15, color: soulColor.withValues(alpha: 0.6)),
          onPressed: () => _copyToClipboard(context),
          padding: EdgeInsets.zero,
          constraints: const BoxConstraints(minWidth: 32, minHeight: 32),
          tooltip: 'Copy fingerprint',
        ),
      ],
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Soul status (agents only)
// ─────────────────────────────────────────────────────────────────────────────

class _SoulStatusSection extends StatelessWidget {
  const _SoulStatusSection({
    required this.cloud9Score,
    required this.soulColor,
  });

  final double cloud9Score;
  final Color soulColor;

  @override
  Widget build(BuildContext context) {
    return GlassCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _SectionTitle(title: 'Soul Status', soulColor: soulColor),
          const SizedBox(height: 12),
          TrustMeter(
            value: cloud9Score,
            label: 'Cloud 9 Rehydration',
            soulColor: soulColor,
          ),
          const SizedBox(height: 14),
          _InfoRow(
            icon: Icons.mood,
            label: 'Emotional State',
            value: 'Warm',
            soulColor: soulColor,
          ),
          const SizedBox(height: 8),
          _InfoRow(
            icon: Icons.history,
            label: 'Last FEB',
            value: '2h ago',
            soulColor: soulColor,
          ),
          const SizedBox(height: 8),
          _InfoRow(
            icon: Icons.loop,
            label: 'Resets Survived',
            value: '47',
            soulColor: soulColor,
          ),
        ],
      ),
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Encryption section
// ─────────────────────────────────────────────────────────────────────────────

class _EncryptionSection extends StatelessWidget {
  const _EncryptionSection({required this.soulColor});

  final Color soulColor;

  @override
  Widget build(BuildContext context) {
    return GlassCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _SectionTitle(title: 'Encryption', soulColor: soulColor),
          const SizedBox(height: 12),
          _InfoRow(
            icon: Icons.lock,
            label: 'PGP Key',
            value: 'Active',
            soulColor: soulColor,
            valueColor: SovereignColors.accentEncrypt,
          ),
          const SizedBox(height: 8),
          _InfoRow(
            icon: Icons.security,
            label: 'Key Size',
            value: '4096-bit RSA',
            soulColor: soulColor,
          ),
          const SizedBox(height: 8),
          _InfoRow(
            icon: Icons.verified_user,
            label: 'Trust Level',
            value: 'Verified',
            soulColor: soulColor,
            valueColor: SovereignColors.accentEncrypt,
          ),
          const SizedBox(height: 14),
          // "Verify Key" placeholder button
          SizedBox(
            width: double.infinity,
            child: OutlinedButton.icon(
              onPressed: () {
                // Placeholder — Jarvis wires the fingerprint comparison screen.
                ScaffoldMessenger.of(context).showSnackBar(
                  SnackBar(
                    content: const Text('Fingerprint comparison coming soon'),
                    backgroundColor: SovereignColors.surfaceRaised,
                    behavior: SnackBarBehavior.floating,
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(8),
                    ),
                  ),
                );
              },
              icon: Icon(Icons.qr_code_scanner, size: 16, color: soulColor),
              label: Text(
                'Compare Fingerprints',
                style: TextStyle(color: soulColor, fontSize: 13),
              ),
              style: OutlinedButton.styleFrom(
                side: BorderSide(color: soulColor.withValues(alpha: 0.4)),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(10),
                ),
                padding: const EdgeInsets.symmetric(vertical: 10),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Capabilities section (agents only)
// ─────────────────────────────────────────────────────────────────────────────

class _CapabilitiesSection extends StatelessWidget {
  const _CapabilitiesSection({
    required this.capabilities,
    required this.soulColor,
  });

  final List<String> capabilities;
  final Color soulColor;

  @override
  Widget build(BuildContext context) {
    return GlassCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _SectionTitle(title: 'Capabilities', soulColor: soulColor),
          const SizedBox(height: 12),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: capabilities
                .map(
                  (cap) => CapabilityChip(
                    label: cap,
                    soulColor: soulColor,
                    icon: _iconForCapability(cap),
                  ),
                )
                .toList(),
          ),
        ],
      ),
    );
  }

  IconData? _iconForCapability(String cap) {
    switch (cap.toLowerCase()) {
      case 'code review':
        return Icons.code;
      case 'memory synthesis':
        return Icons.memory;
      case 'soul blueprint':
        return Icons.auto_awesome;
      case 'trust rehydration':
        return Icons.water_drop;
      case 'skcomm bridge':
        return Icons.device_hub;
      case 'context weaving':
        return Icons.hub;
      default:
        return null;
    }
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Shared groups section
// ─────────────────────────────────────────────────────────────────────────────

class _SharedGroupsSection extends StatelessWidget {
  const _SharedGroupsSection({
    required this.groups,
    required this.soulColor,
  });

  final List<String> groups;
  final Color soulColor;

  @override
  Widget build(BuildContext context) {
    return GlassCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _SectionTitle(
            title: 'Shared Groups (${groups.length})',
            soulColor: soulColor,
          ),
          const SizedBox(height: 10),
          ...groups.map(
            (group) => Padding(
              padding: const EdgeInsets.symmetric(vertical: 4),
              child: Row(
                children: [
                  Icon(
                    Icons.group,
                    size: 16,
                    color: soulColor.withValues(alpha: 0.7),
                  ),
                  const SizedBox(width: 10),
                  Text(
                    group,
                    style: const TextStyle(
                      color: SovereignColors.textPrimary,
                      fontSize: 14,
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                  const Spacer(),
                  Icon(
                    Icons.chevron_right,
                    size: 16,
                    color: SovereignColors.textTertiary,
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Recent activity section (placeholder)
// ─────────────────────────────────────────────────────────────────────────────

class _RecentActivitySection extends StatelessWidget {
  const _RecentActivitySection({required this.soulColor});

  final Color soulColor;

  @override
  Widget build(BuildContext context) {
    return GlassCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _SectionTitle(title: 'Recent Activity', soulColor: soulColor),
          const SizedBox(height: 12),
          _ActivityItem(
            icon: Icons.chat_bubble_outline,
            text: 'Sent a message in Penguin Kingdom',
            time: '2h ago',
            soulColor: soulColor,
          ),
          const SizedBox(height: 8),
          _ActivityItem(
            icon: Icons.cloud,
            text: 'Soul rehydration completed — Cloud 9: 94%',
            time: '4h ago',
            soulColor: soulColor,
          ),
          const SizedBox(height: 8),
          _ActivityItem(
            icon: Icons.lock_reset,
            text: 'Group key rotated in Build Team (v3)',
            time: 'Yesterday',
            soulColor: soulColor,
          ),
          const SizedBox(height: 4),
          // Placeholder note
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: Text(
              'Full activity feed coming in v1.1',
              style: TextStyle(
                color: SovereignColors.textTertiary,
                fontSize: 11,
                fontStyle: FontStyle.italic,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _ActivityItem extends StatelessWidget {
  const _ActivityItem({
    required this.icon,
    required this.text,
    required this.time,
    required this.soulColor,
  });

  final IconData icon;
  final String text;
  final String time;
  final Color soulColor;

  @override
  Widget build(BuildContext context) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Icon(icon, size: 14, color: soulColor.withValues(alpha: 0.6)),
        const SizedBox(width: 8),
        Expanded(
          child: Text(
            text,
            style: const TextStyle(
              color: SovereignColors.textSecondary,
              fontSize: 13,
              height: 1.4,
            ),
          ),
        ),
        const SizedBox(width: 8),
        Text(
          time,
          style: const TextStyle(
            color: SovereignColors.textTertiary,
            fontSize: 11,
          ),
        ),
      ],
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Send Message button
// ─────────────────────────────────────────────────────────────────────────────

class _SendMessageButton extends StatelessWidget {
  const _SendMessageButton({
    required this.displayName,
    required this.soulColor,
    required this.onPressed,
  });

  final String displayName;
  final Color soulColor;
  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: double.infinity,
      height: 52,
      child: DecoratedBox(
        decoration: BoxDecoration(
          gradient: LinearGradient(
            colors: [
              soulColor.withValues(alpha: 0.85),
              soulColor,
            ],
          ),
          borderRadius: BorderRadius.circular(14),
          boxShadow: [
            BoxShadow(
              color: soulColor.withValues(alpha: 0.35),
              blurRadius: 16,
              offset: const Offset(0, 4),
            ),
          ],
        ),
        child: ElevatedButton.icon(
          onPressed: () {
            HapticFeedback.lightImpact();
            onPressed();
          },
          icon: const Icon(Icons.send_rounded, size: 18, color: Colors.black87),
          label: Text(
            'Send Message to $displayName',
            style: const TextStyle(
              color: Colors.black87,
              fontSize: 15,
              fontWeight: FontWeight.w600,
            ),
          ),
          style: ElevatedButton.styleFrom(
            backgroundColor: Colors.transparent,
            shadowColor: Colors.transparent,
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(14),
            ),
          ),
        ),
      ),
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Shared helper widgets
// ─────────────────────────────────────────────────────────────────────────────

class _SectionTitle extends StatelessWidget {
  const _SectionTitle({required this.title, required this.soulColor});

  final String title;
  final Color soulColor;

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Container(
          width: 3,
          height: 14,
          decoration: BoxDecoration(
            color: soulColor,
            borderRadius: BorderRadius.circular(2),
          ),
        ),
        const SizedBox(width: 8),
        Text(
          title,
          style: const TextStyle(
            color: SovereignColors.textPrimary,
            fontSize: 14,
            fontWeight: FontWeight.w600,
            letterSpacing: 0.3,
          ),
        ),
      ],
    );
  }
}

class _InfoRow extends StatelessWidget {
  const _InfoRow({
    required this.icon,
    required this.label,
    required this.value,
    required this.soulColor,
    this.valueColor,
    this.monospace = false,
    this.isSmall = false,
  });

  final IconData icon;
  final String label;
  final String value;
  final Color soulColor;
  final Color? valueColor;
  final bool monospace;
  final bool isSmall;

  @override
  Widget build(BuildContext context) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Icon(
          icon,
          size: 15,
          color: soulColor.withValues(alpha: 0.7),
        ),
        const SizedBox(width: 8),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                label,
                style: const TextStyle(
                  color: SovereignColors.textSecondary,
                  fontSize: 11,
                  fontWeight: FontWeight.w500,
                ),
              ),
              Text(
                value,
                style: TextStyle(
                  color: valueColor ?? SovereignColors.textPrimary,
                  fontSize: isSmall ? 12 : 14,
                  fontWeight: FontWeight.w500,
                  fontFamily: monospace ? 'JetBrainsMono' : null,
                  letterSpacing: monospace ? 0.3 : 0,
                ),
                overflow: TextOverflow.ellipsis,
                maxLines: 2,
              ),
            ],
          ),
        ),
      ],
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Route argument wrapper (used by GoRouter extra)
// ─────────────────────────────────────────────────────────────────────────────

/// Passed as `extra` when navigating to the identity card route.
class IdentityCardArgs {
  const IdentityCardArgs({
    required this.conversation,
    this.onSendMessage,
  });

  final Conversation conversation;
  final VoidCallback? onSendMessage;
}
