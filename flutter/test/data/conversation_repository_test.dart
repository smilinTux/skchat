import 'package:flutter_test/flutter_test.dart';
import 'package:skchat/data/conversation_repository.dart';

void main() {
  group('ConversationRepository', () {
    test('can be instantiated', () {
      final repo = ConversationRepository();
      expect(repo, isNotNull);
    });

    test('provider creates instance', () {
      expect(conversationRepositoryProvider, isNotNull);
    });
  });
}
