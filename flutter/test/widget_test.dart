import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:skchat/main.dart';

void main() {
  testWidgets('SKChatApp smoke test', (WidgetTester tester) async {
    await tester.pumpWidget(
      const ProviderScope(child: SKChatApp()),
    );
    await tester.pump();
    // App renders without crashing.
    expect(find.byType(MaterialApp), findsOneWidget);
  });
}
