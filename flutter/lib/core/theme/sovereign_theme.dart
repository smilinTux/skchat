import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'sovereign_colors.dart';
import 'sovereign_typography.dart';

/// Builds the Sovereign Glass ThemeData for dark and light modes.
class SovereignTheme {
  SovereignTheme._();

  static ThemeData dark() {
    final colorScheme = ColorScheme(
      brightness: Brightness.dark,
      primary: SovereignColors.soulLumina,
      onPrimary: SovereignColors.surfaceBase,
      primaryContainer: SovereignColors.soulLumina.withValues(alpha: 0.15),
      onPrimaryContainer: SovereignColors.soulLumina,
      secondary: SovereignColors.soulJarvis,
      onSecondary: SovereignColors.surfaceBase,
      secondaryContainer: SovereignColors.soulJarvis.withValues(alpha: 0.15),
      onSecondaryContainer: SovereignColors.soulJarvis,
      tertiary: SovereignColors.soulChef,
      onTertiary: SovereignColors.surfaceBase,
      tertiaryContainer: SovereignColors.soulChef.withValues(alpha: 0.15),
      onTertiaryContainer: SovereignColors.soulChef,
      error: SovereignColors.accentDanger,
      onError: Colors.white,
      errorContainer: SovereignColors.accentDanger.withValues(alpha: 0.15),
      onErrorContainer: SovereignColors.accentDanger,
      surface: SovereignColors.surfaceBase,
      onSurface: SovereignColors.textPrimary,
      surfaceContainerHighest: SovereignColors.surfaceRaised,
      onSurfaceVariant: SovereignColors.textSecondary,
      outline: SovereignColors.surfaceGlassBorder,
      outlineVariant: SovereignColors.surfaceGlass,
      inverseSurface: SovereignColors.textPrimary,
      onInverseSurface: SovereignColors.surfaceBase,
      inversePrimary: SovereignColors.soulLumina,
      shadow: Colors.black,
      scrim: Colors.black87,
      surfaceTint: Colors.transparent,
    );

    final textTheme = SovereignTypography.buildTextTheme(dark: true);

    return ThemeData(
      useMaterial3: true,
      brightness: Brightness.dark,
      colorScheme: colorScheme,
      scaffoldBackgroundColor: SovereignColors.surfaceBase,
      textTheme: textTheme,

      // AppBar — transparent glass
      appBarTheme: AppBarTheme(
        backgroundColor: Colors.transparent,
        foregroundColor: SovereignColors.textPrimary,
        elevation: 0,
        scrolledUnderElevation: 0,
        centerTitle: false,
        titleTextStyle: textTheme.titleLarge,
        systemOverlayStyle: const SystemUiOverlayStyle(
          statusBarBrightness: Brightness.dark,
          statusBarIconBrightness: Brightness.light,
          systemNavigationBarColor: Colors.transparent,
          systemNavigationBarIconBrightness: Brightness.light,
        ),
        iconTheme: const IconThemeData(color: SovereignColors.textPrimary),
        actionsIconTheme: const IconThemeData(
          color: SovereignColors.textSecondary,
        ),
      ),

      // Bottom nav bar — glass surface
      bottomNavigationBarTheme: const BottomNavigationBarThemeData(
        backgroundColor: Colors.transparent,
        elevation: 0,
        selectedItemColor: SovereignColors.soulLumina,
        unselectedItemColor: SovereignColors.textSecondary,
        type: BottomNavigationBarType.fixed,
        showSelectedLabels: true,
        showUnselectedLabels: true,
      ),

      // NavigationBar (Material 3)
      navigationBarTheme: NavigationBarThemeData(
        backgroundColor: Colors.transparent,
        indicatorColor: SovereignColors.soulLumina.withValues(alpha: 0.15),
        iconTheme: WidgetStateProperty.resolveWith((states) {
          if (states.contains(WidgetState.selected)) {
            return const IconThemeData(
              color: SovereignColors.soulLumina,
              size: 24,
            );
          }
          return const IconThemeData(
            color: SovereignColors.textSecondary,
            size: 24,
          );
        }),
        labelTextStyle: WidgetStateProperty.resolveWith((states) {
          if (states.contains(WidgetState.selected)) {
            return textTheme.labelSmall?.copyWith(
              color: SovereignColors.soulLumina,
              fontWeight: FontWeight.w600,
            );
          }
          return textTheme.labelSmall;
        }),
      ),

      // Card — glass surface
      cardTheme: CardThemeData(
        color: SovereignColors.surfaceGlass,
        elevation: 0,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(16),
          side: const BorderSide(
            color: SovereignColors.surfaceGlassBorder,
            width: 1,
          ),
        ),
        margin: EdgeInsets.zero,
      ),

      // Input decoration
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: SovereignColors.surfaceGlass,
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: const BorderSide(
            color: SovereignColors.surfaceGlassBorder,
          ),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: const BorderSide(
            color: SovereignColors.surfaceGlassBorder,
          ),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: const BorderSide(
            color: SovereignColors.soulLumina,
            width: 1.5,
          ),
        ),
        hintStyle: textTheme.bodyMedium?.copyWith(
          color: SovereignColors.textTertiary,
        ),
        contentPadding: const EdgeInsets.symmetric(
          horizontal: 16,
          vertical: 12,
        ),
      ),

      // Icon theme
      iconTheme: const IconThemeData(
        color: SovereignColors.textSecondary,
        size: 24,
      ),

      // Divider
      dividerTheme: const DividerThemeData(
        color: SovereignColors.surfaceGlassBorder,
        thickness: 1,
        space: 1,
      ),

      // Chip
      chipTheme: ChipThemeData(
        backgroundColor: SovereignColors.surfaceGlass,
        side: const BorderSide(color: SovereignColors.surfaceGlassBorder),
        labelStyle: textTheme.labelMedium,
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      ),

      // Dialog
      dialogTheme: DialogThemeData(
        backgroundColor: SovereignColors.surfaceRaised,
        elevation: 24,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(20)),
        titleTextStyle: textTheme.titleLarge,
        contentTextStyle: textTheme.bodyMedium,
      ),

      // Floating action button
      floatingActionButtonTheme: const FloatingActionButtonThemeData(
        backgroundColor: SovereignColors.soulLumina,
        foregroundColor: Colors.black,
        elevation: 8,
        shape: CircleBorder(),
      ),

      // Switch / Checkbox / Radio
      switchTheme: SwitchThemeData(
        thumbColor: WidgetStateProperty.resolveWith((states) {
          if (states.contains(WidgetState.selected)) {
            return SovereignColors.soulLumina;
          }
          return SovereignColors.textTertiary;
        }),
        trackColor: WidgetStateProperty.resolveWith((states) {
          if (states.contains(WidgetState.selected)) {
            return SovereignColors.soulLumina.withValues(alpha: 0.3);
          }
          return SovereignColors.surfaceGlassBorder;
        }),
      ),

      // List tile
      listTileTheme: const ListTileThemeData(
        contentPadding: EdgeInsets.symmetric(horizontal: 16, vertical: 4),
        iconColor: SovereignColors.textSecondary,
        textColor: SovereignColors.textPrimary,
      ),

      // Snack bar
      snackBarTheme: SnackBarThemeData(
        backgroundColor: SovereignColors.surfaceRaised,
        contentTextStyle: textTheme.bodyMedium,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        behavior: SnackBarBehavior.floating,
      ),

      // Page transitions — spring-based
      pageTransitionsTheme: const PageTransitionsTheme(
        builders: {
          TargetPlatform.android: ZoomPageTransitionsBuilder(),
          TargetPlatform.iOS: CupertinoPageTransitionsBuilder(),
          TargetPlatform.linux: FadeUpwardsPageTransitionsBuilder(),
          TargetPlatform.macOS: CupertinoPageTransitionsBuilder(),
          TargetPlatform.windows: ZoomPageTransitionsBuilder(),
        },
      ),
    );
  }

  static ThemeData light() {
    final base = dark();
    final colorScheme = base.colorScheme.copyWith(
      brightness: Brightness.light,
      surface: SovereignColors.surfaceBaseLight,
      onSurface: const Color(0xFF1A1A2E),
      surfaceContainerHighest: SovereignColors.surfaceRaisedLight,
      onSurfaceVariant: const Color(0xFF606080),
      outline: SovereignColors.surfaceGlassBorderLight,
      outlineVariant: SovereignColors.surfaceGlassLight,
    );
    final textTheme = SovereignTypography.buildTextTheme(dark: false);
    return base.copyWith(
      brightness: Brightness.light,
      colorScheme: colorScheme,
      scaffoldBackgroundColor: SovereignColors.surfaceBaseLight,
      textTheme: textTheme,
      appBarTheme: base.appBarTheme.copyWith(
        foregroundColor: const Color(0xFF1A1A2E),
        systemOverlayStyle: const SystemUiOverlayStyle(
          statusBarBrightness: Brightness.light,
          statusBarIconBrightness: Brightness.dark,
        ),
      ),
    );
  }
}
