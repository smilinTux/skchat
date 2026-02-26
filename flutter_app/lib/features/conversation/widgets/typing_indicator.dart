import 'package:flutter/material.dart';

import '../../core/theme/glass_decorations.dart';
import '../../core/theme/sovereign_glass.dart';

class TypingIndicator extends StatefulWidget {
  final String name;
  final Color soulColor;
  final bool isAgent;

  const TypingIndicator({
    super.key,
    required this.name,
    required this.soulColor,
    this.isAgent = false,
  });

  @override
  State<TypingIndicator> createState() => _TypingIndicatorState();
}

class _TypingIndicatorState extends State<TypingIndicator>
    with SingleTickerProviderStateMixin {
  late AnimationController _controller;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1200),
    )..repeat();
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8, horizontal: 16),
      child: Row(
        children: [
          GlassDecorations.pill(
            color: widget.soulColor.withValues(alpha: 0.1),
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (widget.isAgent)
                  _buildAgentIndicator()
                else
                  _buildDefaultIndicator(),
                const SizedBox(width: 8),
                Text(
                  '${widget.name} is ${_getTypingText()}',
                  style: TextStyle(
                    fontSize: 13,
                    color: widget.soulColor,
                    fontStyle: FontStyle.italic,
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildDefaultIndicator() {
    return AnimatedBuilder(
      animation: _controller,
      builder: (context, child) {
        return Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            _buildDot(0),
            const SizedBox(width: 4),
            _buildDot(0.33),
            const SizedBox(width: 4),
            _buildDot(0.66),
          ],
        );
      },
    );
  }

  Widget _buildDot(double delay) {
    final value = (_controller.value + delay) % 1.0;
    final opacity = (value < 0.5)
        ? Curves.easeInOut.transform(value * 2)
        : Curves.easeInOut.transform((1 - value) * 2);

    return Container(
      width: 6,
      height: 6,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        color: widget.soulColor.withValues(alpha: 0.3 + opacity * 0.7),
      ),
    );
  }

  Widget _buildAgentIndicator() {
    // Different indicator based on agent name
    if (widget.name.toLowerCase() == 'lumina') {
      return _buildGentleIndicator();
    } else if (widget.name.toLowerCase() == 'jarvis') {
      return _buildSharpIndicator();
    }
    return _buildDefaultIndicator();
  }

  Widget _buildGentleIndicator() {
    return AnimatedBuilder(
      animation: _controller,
      builder: (context, child) {
        return Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            _buildGlowDot(0, 8),
            const SizedBox(width: 6),
            _buildGlowDot(0.33, 8),
            const SizedBox(width: 6),
            _buildGlowDot(0.66, 8),
          ],
        );
      },
    );
  }

  Widget _buildGlowDot(double delay, double size) {
    final value = (_controller.value + delay) % 1.0;
    final scale = 0.6 + (value < 0.5 ? value : 1 - value) * 0.8;

    return Transform.scale(
      scale: scale,
      child: Container(
        width: size,
        height: size,
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: widget.soulColor,
          boxShadow: [
            BoxShadow(
              color: widget.soulColor.withValues(alpha: 0.4 * scale),
              blurRadius: 8 * scale,
              spreadRadius: 2 * scale,
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildSharpIndicator() {
    return AnimatedBuilder(
      animation: _controller,
      builder: (context, child) {
        final isVisible = (_controller.value * 2.5) % 1.0 < 0.5;
        return AnimatedOpacity(
          opacity: isVisible ? 1.0 : 0.0,
          duration: const Duration(milliseconds: 100),
          child: Container(
            width: 2,
            height: 16,
            decoration: BoxDecoration(
              color: widget.soulColor,
              borderRadius: BorderRadius.circular(1),
            ),
          ),
        );
      },
    );
  }

  String _getTypingText() {
    if (widget.isAgent) {
      if (widget.name.toLowerCase() == 'lumina') {
        return 'composing';
      } else if (widget.name.toLowerCase() == 'jarvis') {
        return 'coding';
      }
    }
    return 'typing';
  }
}
