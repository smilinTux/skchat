import 'dart:ui';
import 'package:flutter/material.dart';
import 'sovereign_colors.dart';

/// GlassCard — frosted glass panel with 8-16px blur, subtle border.
class GlassCard extends StatelessWidget {
  const GlassCard({
    super.key,
    required this.child,
    this.blur = 12.0,
    this.opacity = 0.06,
    this.borderOpacity = 0.08,
    this.borderRadius = 16.0,
    this.padding = const EdgeInsets.all(16),
    this.margin = EdgeInsets.zero,
    this.onTap,
  });

  final Widget child;
  final double blur;
  final double opacity;
  final double borderOpacity;
  final double borderRadius;
  final EdgeInsets padding;
  final EdgeInsets margin;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: margin,
      child: ClipRRect(
        borderRadius: BorderRadius.circular(borderRadius),
        child: BackdropFilter(
          filter: ImageFilter.blur(sigmaX: blur, sigmaY: blur),
          child: GestureDetector(
            onTap: onTap,
            child: Container(
              decoration: BoxDecoration(
                color: Color.fromRGBO(
                  255,
                  255,
                  255,
                  opacity,
                ),
                borderRadius: BorderRadius.circular(borderRadius),
                border: Border.all(
                  color: Color.fromRGBO(255, 255, 255, borderOpacity),
                  width: 1,
                ),
              ),
              padding: padding,
              child: child,
            ),
          ),
        ),
      ),
    );
  }
}

/// GlassNavBar — frosted bottom navigation bar.
class GlassNavBar extends StatelessWidget {
  const GlassNavBar({
    super.key,
    required this.child,
    this.height = 80,
    this.blur = 16.0,
  });

  final Widget child;
  final double height;
  final double blur;

  @override
  Widget build(BuildContext context) {
    final bottomPadding = MediaQuery.of(context).padding.bottom;
    return ClipRect(
      child: BackdropFilter(
        filter: ImageFilter.blur(sigmaX: blur, sigmaY: blur),
        child: Container(
          height: height + bottomPadding,
          decoration: const BoxDecoration(
            color: SovereignColors.surfaceGlass,
            border: Border(
              top: BorderSide(
                color: SovereignColors.surfaceGlassBorder,
                width: 1,
              ),
            ),
          ),
          padding: EdgeInsets.only(bottom: bottomPadding),
          child: child,
        ),
      ),
    );
  }
}

/// SoulAvatar — circular avatar with a soul-color ring.
/// Shows a pulsing animation when [isOnline] is true.
/// AI agents get a diamond badge overlay.
class SoulAvatar extends StatefulWidget {
  const SoulAvatar({
    super.key,
    required this.soulColor,
    this.initials,
    this.imageUrl,
    this.size = 48.0,
    this.isOnline = false,
    this.isAgent = false,
    this.ringWidth = 2.0,
  });

  final Color soulColor;
  final String? initials;
  final String? imageUrl;
  final double size;
  final bool isOnline;
  final bool isAgent;
  final double ringWidth;

  @override
  State<SoulAvatar> createState() => _SoulAvatarState();
}

class _SoulAvatarState extends State<SoulAvatar>
    with SingleTickerProviderStateMixin {
  late AnimationController _pulseController;
  late Animation<double> _pulseAnimation;

  @override
  void initState() {
    super.initState();
    _pulseController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1800),
    );
    _pulseAnimation = Tween<double>(begin: 1.0, end: 1.15).animate(
      CurvedAnimation(parent: _pulseController, curve: Curves.easeInOut),
    );
    if (widget.isOnline) {
      _pulseController.repeat(reverse: true);
    }
  }

  @override
  void didUpdateWidget(SoulAvatar oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.isOnline && !_pulseController.isAnimating) {
      _pulseController.repeat(reverse: true);
    } else if (!widget.isOnline) {
      _pulseController.stop();
      _pulseController.reset();
    }
  }

  @override
  void dispose() {
    _pulseController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final innerSize = widget.size - widget.ringWidth * 2 - 4;

    return AnimatedBuilder(
      animation: _pulseAnimation,
      builder: (context, child) {
        return Transform.scale(
          scale: widget.isOnline ? _pulseAnimation.value : 1.0,
          child: Container(
            width: widget.size,
            height: widget.size,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              border: Border.all(
                color: widget.soulColor.withValues(alpha: widget.isOnline ? 0.8 : 0.4),
                width: widget.ringWidth,
              ),
            ),
            child: Center(
              child: Stack(
                clipBehavior: Clip.none,
                children: [
                  // Avatar circle
                  Container(
                    width: innerSize,
                    height: innerSize,
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      color: widget.soulColor.withValues(alpha: 0.2),
                      image: widget.imageUrl != null
                          ? DecorationImage(
                              image: NetworkImage(widget.imageUrl!),
                              fit: BoxFit.cover,
                            )
                          : null,
                    ),
                    child: widget.imageUrl == null
                        ? Center(
                            child: Text(
                              widget.initials ?? '?',
                              style: TextStyle(
                                color: widget.soulColor,
                                fontSize: innerSize * 0.36,
                                fontWeight: FontWeight.w700,
                              ),
                            ),
                          )
                        : null,
                  ),
                  // Agent diamond badge
                  if (widget.isAgent)
                    Positioned(
                      right: -2,
                      bottom: -2,
                      child: Container(
                        width: 14,
                        height: 14,
                        decoration: BoxDecoration(
                          color: widget.soulColor,
                          borderRadius: BorderRadius.circular(3),
                        ),
                        child: const Center(
                          child: Text(
                            '◆',
                            style: TextStyle(fontSize: 7, color: Colors.black),
                          ),
                        ),
                      ),
                    ),
                ],
              ),
            ),
          ),
        );
      },
    );
  }
}

/// EncryptBadge — tiny lock icon indicating E2E encryption.
class EncryptBadge extends StatelessWidget {
  const EncryptBadge({super.key, this.size = 12.0});

  final double size;

  @override
  Widget build(BuildContext context) {
    return Icon(Icons.lock, size: size, color: SovereignColors.accentEncrypt);
  }
}

/// DeliveryStatus — renders ✓ / ✓✓ / ✓✓(colored) based on status string.
class DeliveryStatus extends StatelessWidget {
  const DeliveryStatus({super.key, required this.status, this.soulColor});

  final String status; // 'sent' | 'delivered' | 'read'
  final Color? soulColor;

  @override
  Widget build(BuildContext context) {
    switch (status) {
      case 'read':
        return Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              Icons.done_all,
              size: 14,
              color: soulColor ?? SovereignColors.soulLumina,
            ),
          ],
        );
      case 'delivered':
        return const Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.done_all, size: 14, color: SovereignColors.textSecondary),
          ],
        );
      default: // 'sent'
        return const Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.done, size: 14, color: SovereignColors.textTertiary),
          ],
        );
    }
  }
}
