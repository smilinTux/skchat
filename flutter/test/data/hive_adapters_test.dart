import 'package:flutter_test/flutter_test.dart';
import 'package:skchat/data/hive_adapters.dart';
import 'package:skchat/models/chat_message.dart';
import 'package:skchat/models/conversation.dart';

void main() {
  group('ChatMessageAdapter', () {
    test('has correct typeId', () {
      final adapter = ChatMessageAdapter();
      expect(adapter.typeId, chatMessageTypeId);
      expect(adapter.typeId, 0);
    });
  });

  group('ConversationAdapter', () {
    test('has correct typeId', () {
      final adapter = ConversationAdapter();
      expect(adapter.typeId, conversationTypeId);
      expect(adapter.typeId, 1);
    });
  });

  group('typeId uniqueness', () {
    test('ChatMessage and Conversation have different type IDs', () {
      expect(chatMessageTypeId, isNot(equals(conversationTypeId)));
    });
  });
}
