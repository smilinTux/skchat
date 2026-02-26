import 'package:flutter/material.dart';

import '../../core/theme/glass_decorations.dart';
import '../../core/theme/sovereign_glass.dart';

class InputBar extends StatefulWidget {
  final Color soulColor;
  final Function(String) onSend;

  const InputBar({
    super.key,
    required this.soulColor,
    required this.onSend,
  });

  @override
  State<InputBar> createState() => _InputBarState();
}

class _InputBarState extends State<InputBar> {
  final _controller = TextEditingController();
  bool _hasText = false;

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  void _handleSend() {
    if (_controller.text.trim().isNotEmpty) {
      widget.onSend(_controller.text.trim());
      _controller.clear();
      setState(() {
        _hasText = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return GlassDecorations.bottomBar(
      height: null,
      child: Padding(
        padding: EdgeInsets.only(
          left: 16,
          right: 16,
          top: 8,
          bottom: 8 + MediaQuery.of(context).padding.bottom,
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            IconButton(
              icon: const Icon(Icons.attach_file),
              onPressed: () {
                // TODO: Attach file
              },
              padding: EdgeInsets.zero,
              constraints: const BoxConstraints(),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: Container(
                constraints: const BoxConstraints(
                  minHeight: 40,
                  maxHeight: 120,
                ),
                decoration: BoxDecoration(
                  color: SovereignGlassTheme.surfaceGlass,
                  borderRadius: BorderRadius.circular(20),
                  border: Border.all(
                    color: SovereignGlassTheme.surfaceGlassBorder,
                    width: 1,
                  ),
                ),
                child: TextField(
                  controller: _controller,
                  maxLines: null,
                  textCapitalization: TextCapitalization.sentences,
                  style: const TextStyle(
                    fontSize: 15,
                    color: SovereignGlassTheme.textPrimary,
                  ),
                  decoration: const InputDecoration(
                    hintText: 'Message...',
                    hintStyle: TextStyle(
                      color: SovereignGlassTheme.textSecondary,
                    ),
                    border: InputBorder.none,
                    contentPadding: EdgeInsets.symmetric(
                      horizontal: 16,
                      vertical: 10,
                    ),
                  ),
                  onChanged: (text) {
                    setState(() {
                      _hasText = text.trim().isNotEmpty;
                    });
                  },
                  onSubmitted: (_) => _handleSend(),
                ),
              ),
            ),
            const SizedBox(width: 8),
            GestureDetector(
              onTap: _hasText ? _handleSend : null,
              onLongPress: () {
                // TODO: Voice message
              },
              child: Container(
                width: 40,
                height: 40,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: _hasText
                      ? widget.soulColor
                      : SovereignGlassTheme.surfaceGlass,
                ),
                child: Icon(
                  _hasText ? Icons.send : Icons.mic,
                  size: 20,
                  color: _hasText
                      ? Colors.white
                      : SovereignGlassTheme.textSecondary,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
