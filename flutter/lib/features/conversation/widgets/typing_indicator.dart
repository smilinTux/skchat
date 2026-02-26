import 'package:flutter/material.dart';
import '../../../core/theme/sovereign_colors.dart';

/// Personality-aware typing indicator per the PRD:
/// - Lumina: gentle pulse with violet glow  · · · Lumina is composing · · ·
/// - Jarvis: sharp cursor blink  ▌ Jarvis is coding...
/// - Human: standard  typing...
class TypingIndicator extends StatefulWidget {
  const TypingIndicator({
    super.key,
    required this.peerName,
    required this.isAgent,
    required this.soulColor,
  });

  final String peerName;
  final bool isAgent;
  final Color soulColor;

  @override
  State<TypingIndicator> createState() => _TypingIndicatorState();
}

class _TypingIndicatorState extends State<TypingIndicator>
    with TickerProviderStateMixin {
  late final List<AnimationController> _dotControllers;
  late final AnimationController _cursorController;
  late final Animation<double> _cursorBlink;

  static const _knownAgents = {'lumina', 'jarvis'};

  String get _agentKey => widget.peerName.toLowerCase();
  bool get _isLumina => _agentKey == 'lumina';
  bool get _isJarvis => _agentKey == 'jarvis';

  @override
  void initState() {
    super.initState();

    // 3 dot pulse controllers for Lumina-style agents
    _dotControllers = List.generate(3, (i) {
      final ctrl = AnimationController(
        vsync: this,
        duration: const Duration(milliseconds: 500),
      );
      Future.delayed(Duration(milliseconds: i * 160), () {
        if (mounted) ctrl.repeat(reverse: true);
      });
      return ctrl;
    });

    // Cursor blink for Jarvis-style agents
    _cursorController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 600),
    )..repeat(reverse: true);
    _cursorBlink = Tween<double>(begin: 0.0, end: 1.0).animate(
      CurvedAnimation(parent: _cursorController, curve: Curves.easeInOut),
    );
  }

  @override
  void dispose() {
    for (final c in _dotControllers) {
      c.dispose();
    }
    _cursorController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (widget.isAgent && _knownAgents.contains(_agentKey)) {
      if (_isLumina) return _buildLuminaStyle();
      if (_isJarvis) return _buildJarvisStyle();
    }
    return _buildHumanStyle();
  }

  Widget _buildLuminaStyle() {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        _PulseDot(controller: _dotControllers[0], color: widget.soulColor),
        const SizedBox(width: 4),
        _PulseDot(controller: _dotControllers[1], color: widget.soulColor),
        const SizedBox(width: 4),
        _PulseDot(controller: _dotControllers[2], color: widget.soulColor),
        const SizedBox(width: 8),
        Text(
          '${widget.peerName} is composing',
          style: TextStyle(
            fontSize: 13,
            fontStyle: FontStyle.italic,
            color: widget.soulColor.withValues(alpha: 0.8),
          ),
        ),
        const SizedBox(width: 8),
        _PulseDot(controller: _dotControllers[2], color: widget.soulColor),
        const SizedBox(width: 4),
        _PulseDot(controller: _dotControllers[1], color: widget.soulColor),
        const SizedBox(width: 4),
        _PulseDot(controller: _dotControllers[0], color: widget.soulColor),
      ],
    );
  }

  Widget _buildJarvisStyle() {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        AnimatedBuilder(
          animation: _cursorBlink,
          builder: (context, _) => Opacity(
            opacity: _cursorBlink.value,
            child: Text(
              '▌',
              style: TextStyle(
                fontSize: 16,
                color: widget.soulColor,
                fontWeight: FontWeight.w700,
              ),
            ),
          ),
        ),
        const SizedBox(width: 6),
        Text(
          '${widget.peerName} is coding...',
          style: TextStyle(
            fontSize: 13,
            fontStyle: FontStyle.italic,
            color: widget.soulColor.withValues(alpha: 0.8),
            fontFamily: 'JetBrainsMono',
          ),
        ),
      ],
    );
  }

  Widget _buildHumanStyle() {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        _PulseDot(
          controller: _dotControllers[0],
          color: SovereignColors.textSecondary,
        ),
        const SizedBox(width: 4),
        _PulseDot(
          controller: _dotControllers[1],
          color: SovereignColors.textSecondary,
        ),
        const SizedBox(width: 4),
        _PulseDot(
          controller: _dotControllers[2],
          color: SovereignColors.textSecondary,
        ),
        const SizedBox(width: 8),
        Text(
          'typing...',
          style: TextStyle(
            fontSize: 13,
            fontStyle: FontStyle.italic,
            color: SovereignColors.textSecondary,
          ),
        ),
      ],
    );
  }
}

class _PulseDot extends AnimatedWidget {
  const _PulseDot({required AnimationController controller, required this.color})
      : super(listenable: controller);

  final Color color;

  @override
  Widget build(BuildContext context) {
    final animation = listenable as Animation<double>;
    return Opacity(
      opacity: 0.3 + animation.value * 0.7,
      child: Container(
        width: 6,
        height: 6,
        decoration: BoxDecoration(color: color, shape: BoxShape.circle),
      ),
    );
  }
}
