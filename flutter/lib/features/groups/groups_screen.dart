import 'package:flutter/material.dart';
import '../../core/theme/theme.dart';

/// Groups screen — placeholder until task ad7b6233.
class GroupsScreen extends StatelessWidget {
  const GroupsScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: SovereignColors.surfaceBase,
      appBar: AppBar(title: const Text('Groups')),
      body: Center(
        child: GlassCard(
          padding: const EdgeInsets.all(24),
          child: Text(
            'Group chats\ncoming soon.',
            textAlign: TextAlign.center,
            style: Theme.of(context).textTheme.bodyLarge?.copyWith(
              color: SovereignColors.textSecondary,
            ),
          ),
        ),
      ),
    );
  }
}
