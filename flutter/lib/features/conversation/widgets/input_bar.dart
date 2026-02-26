import 'dart:ui';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../../../core/theme/sovereign_colors.dart';

/// Glass-surface input bar pinned at the bottom of the conversation screen.
/// Left: attachment button. Center: text field. Right: voice/send.
/// On desktop: Enter sends, Shift+Enter inserts newline.
class InputBar extends StatefulWidget {
  const InputBar({
    super.key,
    required this.onSend,
    this.soulColor = SovereignColors.soulLumina,
  });

  final void Function(String text) onSend;
  final Color soulColor;

  @override
  State<InputBar> createState() => _InputBarState();
}

class _InputBarState extends State<InputBar> {
  final _controller = TextEditingController();
  final _focusNode = FocusNode();
  bool _hasText = false;

  @override
  void initState() {
    super.initState();
    _controller.addListener(() {
      final hasText = _controller.text.trim().isNotEmpty;
      if (hasText != _hasText) {
        setState(() => _hasText = hasText);
      }
    });
  }

  @override
  void dispose() {
    _controller.dispose();
    _focusNode.dispose();
    super.dispose();
  }

  void _submit() {
    final text = _controller.text.trim();
    if (text.isEmpty) return;
    widget.onSend(text);
    _controller.clear();
    _focusNode.requestFocus();
  }

  /// Enter sends on desktop; Shift+Enter inserts a newline.
  KeyEventResult _handleKeyEvent(FocusNode node, KeyEvent event) {
    if (event is KeyDownEvent &&
        event.logicalKey == LogicalKeyboardKey.enter &&
        !HardwareKeyboard.instance.isShiftPressed) {
      _submit();
      return KeyEventResult.handled;
    }
    return KeyEventResult.ignored;
  }

  @override
  Widget build(BuildContext context) {
    final bottomPadding = MediaQuery.of(context).padding.bottom;

    return ClipRect(
      child: BackdropFilter(
        filter: ImageFilter.blur(sigmaX: 16, sigmaY: 16),
        child: Container(
          decoration: const BoxDecoration(
            color: SovereignColors.surfaceGlass,
            border: Border(
              top: BorderSide(
                color: SovereignColors.surfaceGlassBorder,
                width: 1,
              ),
            ),
          ),
          padding: EdgeInsets.fromLTRB(8, 8, 8, 8 + bottomPadding),
          child: SafeArea(
            top: false,
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                // Attachment
                IconButton(
                  icon: const Icon(Icons.attach_file_rounded),
                  color: SovereignColors.textSecondary,
                  onPressed: () {},
                  tooltip: 'Attach file',
                ),

                // Text field with explicit focus management
                Expanded(
                  child: Container(
                    constraints: const BoxConstraints(maxHeight: 120),
                    decoration: BoxDecoration(
                      color: SovereignColors.surfaceRaised,
                      borderRadius: BorderRadius.circular(24),
                      border: Border.all(
                        color: SovereignColors.surfaceGlassBorder,
                      ),
                    ),
                    padding: const EdgeInsets.symmetric(
                      horizontal: 14,
                      vertical: 4,
                    ),
                    child: Focus(
                      onKeyEvent: _handleKeyEvent,
                      child: TextField(
                        controller: _controller,
                        focusNode: _focusNode,
                        autofocus: true,
                        maxLines: null,
                        keyboardType: TextInputType.multiline,
                        textInputAction: TextInputAction.newline,
                        style: const TextStyle(
                          fontSize: 15,
                          color: SovereignColors.textPrimary,
                        ),
                        decoration: const InputDecoration(
                          hintText: 'Message...',
                          hintStyle: TextStyle(
                            color: SovereignColors.textTertiary,
                            fontSize: 15,
                          ),
                          border: InputBorder.none,
                          isDense: true,
                          contentPadding: EdgeInsets.symmetric(vertical: 8),
                        ),
                      ),
                    ),
                  ),
                ),

                const SizedBox(width: 4),

                // Send / Voice button
                AnimatedSwitcher(
                  duration: const Duration(milliseconds: 200),
                  child: _hasText
                      ? _SendButton(
                          key: const ValueKey('send'),
                          onTap: _submit,
                          soulColor: widget.soulColor,
                        )
                      : _VoiceButton(
                          key: const ValueKey('voice'),
                          soulColor: widget.soulColor,
                        ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _SendButton extends StatelessWidget {
  const _SendButton({super.key, required this.onTap, required this.soulColor});

  final VoidCallback onTap;
  final Color soulColor;

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: Container(
        width: 44,
        height: 44,
        decoration: BoxDecoration(
          color: soulColor,
          shape: BoxShape.circle,
        ),
        child: const Icon(Icons.send_rounded, color: Colors.black, size: 20),
      ),
    );
  }
}

class _VoiceButton extends StatelessWidget {
  const _VoiceButton({super.key, required this.soulColor});

  final Color soulColor;

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onLongPress: () {},
      child: Container(
        width: 44,
        height: 44,
        decoration: BoxDecoration(
          color: soulColor.withValues(alpha: 0.15),
          shape: BoxShape.circle,
          border: Border.all(color: soulColor.withValues(alpha: 0.3)),
        ),
        child: Icon(Icons.mic_rounded, color: soulColor, size: 20),
      ),
    );
  }
}
