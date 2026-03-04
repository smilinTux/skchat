import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/theme/sovereign_colors.dart';
import '../../core/theme/glass_widgets.dart';
import 'consciousness_provider.dart';

/// Glass card showing the online/offline status of each LLM backend,
/// plus the total messages processed by the consciousness loop.
///
/// Embed in the agent detail view (IdentityCardScreen) for agents.
class BackendHealthWidget extends ConsumerWidget {
  const BackendHealthWidget({super.key, required this.soulColor});

  final Color soulColor;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final asyncState = ref.watch(consciousnessProvider);

    return asyncState.when(
      loading: () => _BackendHealthCard(
        soulColor: soulColor,
        isLoading: true,
        state: null,
      ),
      error: (_, __) => _BackendHealthCard(
        soulColor: soulColor,
        isLoading: false,
        state: ConsciousnessState.offline(),
      ),
      data: (state) => _BackendHealthCard(
        soulColor: soulColor,
        isLoading: false,
        state: state,
      ),
    );
  }
}

// ── Card implementation ────────────────────────────────────────────────────────

class _BackendHealthCard extends StatelessWidget {
  const _BackendHealthCard({
    required this.soulColor,
    required this.isLoading,
    required this.state,
  });

  final Color soulColor;
  final bool isLoading;
  final ConsciousnessState? state;

  @override
  Widget build(BuildContext context) {
    return GlassCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // ── Header row ────────────────────────────────────────────────
          Row(
            children: [
              // Section colour accent bar
              Container(
                width: 3,
                height: 14,
                decoration: BoxDecoration(
                  color: soulColor,
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
              const SizedBox(width: 8),
              const Text(
                'Backend Health',
                style: TextStyle(
                  color: SovereignColors.textPrimary,
                  fontSize: 14,
                  fontWeight: FontWeight.w600,
                  letterSpacing: 0.3,
                ),
              ),
              const Spacer(),
              if (isLoading)
                SizedBox(
                  width: 14,
                  height: 14,
                  child: CircularProgressIndicator(
                    strokeWidth: 2,
                    color: soulColor,
                  ),
                )
              else if (state != null)
                _MessageCountBadge(
                  count: state!.messagesProcessed,
                  soulColor: soulColor,
                ),
            ],
          ),
          const SizedBox(height: 12),

          // ── Backend rows ──────────────────────────────────────────────
          if (isLoading)
            Center(
              child: Padding(
                padding: const EdgeInsets.symmetric(vertical: 12),
                child: Text(
                  'Checking backends…',
                  style: const TextStyle(
                    color: SovereignColors.textTertiary,
                    fontSize: 13,
                    fontStyle: FontStyle.italic,
                  ),
                ),
              ),
            )
          else if (state != null)
            ...state!.backends.map(
              (b) => Padding(
                padding: const EdgeInsets.only(bottom: 9),
                child: _BackendRow(backend: b),
              ),
            ),

          // ── Consciousness loop status row ─────────────────────────────
          if (!isLoading && state != null) ...[
            const Divider(
              color: SovereignColors.surfaceGlassBorder,
              height: 16,
              thickness: 1,
            ),
            _ConsciousnessLoopRow(status: state!.status, soulColor: soulColor),
          ],
        ],
      ),
    );
  }
}

// ── Individual backend row ────────────────────────────────────────────────────

class _BackendRow extends StatelessWidget {
  const _BackendRow({required this.backend});

  final BackendStatus backend;

  @override
  Widget build(BuildContext context) {
    final color =
        backend.online ? SovereignColors.accentEncrypt : SovereignColors.textTertiary;

    return Row(
      children: [
        // Status dot
        Container(
          width: 8,
          height: 8,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: color,
            boxShadow: backend.online
                ? [
                    BoxShadow(
                      color: color.withValues(alpha: 0.5),
                      blurRadius: 6,
                    )
                  ]
                : null,
          ),
        ),
        const SizedBox(width: 10),
        Text(
          _displayName(backend.name),
          style: const TextStyle(
            color: SovereignColors.textPrimary,
            fontSize: 13,
            fontWeight: FontWeight.w500,
          ),
        ),
        const Spacer(),
        Text(
          backend.online ? 'online' : 'offline',
          style: TextStyle(
            color: color,
            fontSize: 12,
            fontWeight: FontWeight.w500,
          ),
        ),
      ],
    );
  }

  String _displayName(String name) {
    switch (name) {
      case 'ollama':
        return 'Ollama (local)';
      case 'anthropic':
        return 'Anthropic Claude';
      case 'openai':
        return 'OpenAI';
      case 'grok':
        return 'Grok (xAI)';
      case 'kimi':
        return 'Kimi (Moonshot)';
      case 'nvidia':
        return 'NVIDIA NIM';
      case 'passthrough':
        return 'Passthrough';
      default:
        return name;
    }
  }
}

// ── Consciousness loop status row ─────────────────────────────────────────────

class _ConsciousnessLoopRow extends StatelessWidget {
  const _ConsciousnessLoopRow({
    required this.status,
    required this.soulColor,
  });

  final ConsciousnessStatus status;
  final Color soulColor;

  @override
  Widget build(BuildContext context) {
    final (label, color) = switch (status) {
      ConsciousnessStatus.active => ('ACTIVE', SovereignColors.accentEncrypt),
      ConsciousnessStatus.idle => ('IDLE', SovereignColors.accentWarning),
      ConsciousnessStatus.offline => ('OFFLINE', SovereignColors.textTertiary),
    };

    return Row(
      children: [
        Icon(
          Icons.psychology_outlined,
          size: 15,
          color: soulColor.withValues(alpha: 0.7),
        ),
        const SizedBox(width: 8),
        const Text(
          'Consciousness Loop',
          style: TextStyle(
            color: SovereignColors.textSecondary,
            fontSize: 13,
            fontWeight: FontWeight.w500,
          ),
        ),
        const Spacer(),
        _StatusChip(label: label, color: color),
      ],
    );
  }
}

class _StatusChip extends StatelessWidget {
  const _StatusChip({required this.label, required this.color});

  final String label;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 2),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.15),
        borderRadius: BorderRadius.circular(5),
        border: Border.all(color: color.withValues(alpha: 0.35), width: 0.5),
      ),
      child: Text(
        label,
        style: TextStyle(
          color: color,
          fontSize: 10,
          fontWeight: FontWeight.w700,
          letterSpacing: 0.6,
        ),
      ),
    );
  }
}

// ── Message count badge ───────────────────────────────────────────────────────

/// Compact badge showing the total messages processed by the consciousness loop.
class _MessageCountBadge extends StatelessWidget {
  const _MessageCountBadge({required this.count, required this.soulColor});

  final int count;
  final Color soulColor;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: soulColor.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: soulColor.withValues(alpha: 0.3), width: 1),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(Icons.chat_bubble_outline, size: 11, color: soulColor),
          const SizedBox(width: 4),
          Text(
            _formatCount(count),
            style: TextStyle(
              color: soulColor,
              fontSize: 11,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(width: 2),
          Text(
            'msgs',
            style: TextStyle(
              color: soulColor.withValues(alpha: 0.7),
              fontSize: 10,
            ),
          ),
        ],
      ),
    );
  }

  String _formatCount(int n) {
    if (n >= 1000000) return '${(n / 1000000).toStringAsFixed(1)}M';
    if (n >= 1000) return '${(n / 1000).toStringAsFixed(1)}k';
    return '$n';
  }
}

/// Standalone message count indicator widget.
///
/// Displays the total messages processed counter with an icon.
/// Suitable for embedding anywhere outside [BackendHealthWidget].
class MessageCountIndicator extends ConsumerWidget {
  const MessageCountIndicator({super.key, required this.soulColor});

  final Color soulColor;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final asyncState = ref.watch(consciousnessProvider);

    return asyncState.when(
      loading: () => const SizedBox.shrink(),
      error: (_, __) => const SizedBox.shrink(),
      data: (state) => _MessageCountBadge(
        count: state.messagesProcessed,
        soulColor: soulColor,
      ),
    );
  }
}
