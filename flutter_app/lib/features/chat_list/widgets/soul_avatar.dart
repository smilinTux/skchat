import 'package:flutter/material.dart';

import '../../core/theme/soul_color.dart';

class SoulAvatar extends StatelessWidget {
  final String name;
  final Color soulColor;
  final bool isOnline;
  final bool isAgent;
  final double size;

  const SoulAvatar({
    super.key,
    required this.name,
    required this.soulColor,
    this.isOnline = false,
    this.isAgent = false,
    this.size = 48,
  });

  @override
  Widget build(BuildContext context) {
    return Stack(
      children: [
        Container(
          width: size,
          height: size,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            gradient: SoulColor.avatarGradient(soulColor, isOnline: isOnline),
            boxShadow: isOnline ? SoulColor.onlineGlow(soulColor) : null,
          ),
          child: Center(
            child: Text(
              name[0].toUpperCase(),
              style: TextStyle(
                fontSize: size * 0.4,
                fontWeight: FontWeight.w600,
                color: Colors.white,
              ),
            ),
          ),
        ),
        if (isAgent)
          Positioned(
            right: 0,
            bottom: 0,
            child: Container(
              width: size * 0.3,
              height: size * 0.3,
              decoration: BoxDecoration(
                color: soulColor,
                shape: BoxShape.circle,
                border: Border.all(
                  color: Colors.black,
                  width: 2,
                ),
              ),
              child: Icon(
                Icons.diamond,
                size: size * 0.18,
                color: Colors.white,
              ),
            ),
          ),
      ],
    );
  }
}
