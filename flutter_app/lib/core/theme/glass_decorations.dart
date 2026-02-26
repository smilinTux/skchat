import 'dart:ui';
import 'package:flutter/material.dart';

import 'sovereign_glass.dart';

/// Reusable glass surface decoration builders
class GlassDecorations {
  /// Glass surface with backdrop blur
  static Widget surface({
    required Widget child,
    double? radius,
    Color? color,
    bool showBorder = true,
    EdgeInsets? padding,
  }) {
    return Container(
      padding: padding,
      decoration: SovereignGlassTheme.glassDecoration(
        color: color,
        radius: radius,
        showBorder: showBorder,
      ),
      child: ClipRRect(
        borderRadius: BorderRadius.circular(
          radius ?? SovereignGlassTheme.borderRadius,
        ),
        child: BackdropFilter(
          filter: ImageFilter.blur(
            sigmaX: SovereignGlassTheme.glassBlurSigma,
            sigmaY: SovereignGlassTheme.glassBlurSigma,
          ),
          child: child,
        ),
      ),
    );
  }
  
  /// Glass bottom navigation bar
  static Widget bottomBar({
    required Widget child,
    double height = 72,
  }) {
    return Container(
      height: height,
      decoration: const BoxDecoration(
        color: SovereignGlassTheme.surfaceGlass,
        border: Border(
          top: BorderSide(
            color: SovereignGlassTheme.surfaceGlassBorder,
            width: 1,
          ),
        ),
      ),
      child: ClipRect(
        child: BackdropFilter(
          filter: ImageFilter.blur(
            sigmaX: SovereignGlassTheme.glassBlurSigma,
            sigmaY: SovereignGlassTheme.glassBlurSigma,
          ),
          child: child,
        ),
      ),
    );
  }
  
  /// Glass modal bottom sheet
  static Widget modalSheet({
    required Widget child,
    double? height,
  }) {
    return Container(
      height: height,
      decoration: const BoxDecoration(
        color: SovereignGlassTheme.surfaceGlass,
        borderRadius: BorderRadius.vertical(
          top: Radius.circular(SovereignGlassTheme.borderRadius),
        ),
        border: Border(
          top: BorderSide(
            color: SovereignGlassTheme.surfaceGlassBorder,
            width: 1,
          ),
        ),
      ),
      child: ClipRRect(
        borderRadius: const BorderRadius.vertical(
          top: Radius.circular(SovereignGlassTheme.borderRadius),
        ),
        child: BackdropFilter(
          filter: ImageFilter.blur(
            sigmaX: SovereignGlassTheme.glassBlurSigma,
            sigmaY: SovereignGlassTheme.glassBlurSigma,
          ),
          child: child,
        ),
      ),
    );
  }
  
  /// Glass pill shape (for chips, tags, etc)
  static Widget pill({
    required Widget child,
    Color? color,
    EdgeInsets? padding,
  }) {
    return Container(
      padding: padding ?? const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      decoration: BoxDecoration(
        color: color ?? SovereignGlassTheme.surfaceGlass,
        borderRadius: BorderRadius.circular(100),
        border: Border.all(
          color: SovereignGlassTheme.surfaceGlassBorder,
          width: 1,
        ),
      ),
      child: ClipRRect(
        borderRadius: BorderRadius.circular(100),
        child: BackdropFilter(
          filter: ImageFilter.blur(
            sigmaX: 8,
            sigmaY: 8,
          ),
          child: child,
        ),
      ),
    );
  }
  
  /// Glass app bar with blur
  static PreferredSizeWidget appBar({
    required String title,
    List<Widget>? actions,
    Widget? leading,
    PreferredSizeWidget? bottom,
  }) {
    return AppBar(
      backgroundColor: Colors.transparent,
      elevation: 0,
      surfaceTintColor: Colors.transparent,
      leading: leading,
      title: Text(title),
      actions: actions,
      bottom: bottom,
      flexibleSpace: ClipRect(
        child: BackdropFilter(
          filter: ImageFilter.blur(
            sigmaX: SovereignGlassTheme.glassBlurSigma,
            sigmaY: SovereignGlassTheme.glassBlurSigma,
          ),
          child: Container(
            decoration: const BoxDecoration(
              color: SovereignGlassTheme.surfaceGlass,
              border: Border(
                bottom: BorderSide(
                  color: SovereignGlassTheme.surfaceGlassBorder,
                  width: 1,
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
