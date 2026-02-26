import 'package:flutter/material.dart';
import '../../core/theme/theme.dart';

/// Activity / notifications screen — placeholder.
class ActivityScreen extends StatelessWidget {
  const ActivityScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: SovereignColors.surfaceBase,
      appBar: AppBar(title: const Text('Activity')),
      body: Center(
        child: GlassCard(
          padding: const EdgeInsets.all(24),
          child: Text(
            'Notifications\ncoming soon.',
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
