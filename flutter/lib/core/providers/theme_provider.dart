import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

/// Tracks whether the app is in dark or light mode.
/// Defaults to dark (OLED-first design principle).
class ThemeNotifier extends Notifier<ThemeMode> {
  @override
  ThemeMode build() => ThemeMode.dark;

  void setDark() => state = ThemeMode.dark;
  void setLight() => state = ThemeMode.light;
  void setSystem() => state = ThemeMode.system;

  void toggle() {
    state = state == ThemeMode.dark ? ThemeMode.light : ThemeMode.dark;
  }
}

final themeProvider = NotifierProvider<ThemeNotifier, ThemeMode>(
  ThemeNotifier.new,
);
