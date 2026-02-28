import 'package:flutter_test/flutter_test.dart';
import 'package:skchat/models/chat_message.dart';

void main() {
  group('ChatMessage', () {
    test('default values are set correctly', () {
      final msg = ChatMessage(
        id: 'msg1',
        peerId: 'lumina',
        content: 'Hello',
        timestamp: DateTime(2026, 2, 27),
        isOutbound: true,
      );

      expect(msg.deliveryStatus, 'sent');
      expect(msg.isEncrypted, true);
      expect(msg.replyToId, isNull);
      expect(msg.reactions, isEmpty);
      expect(msg.isAgent, false);
      expect(msg.senderName, isNull);
    });

    test('copyWith preserves unchanged fields', () {
      final original = ChatMessage(
        id: 'msg1',
        peerId: 'jarvis',
        content: 'Deploy complete.',
        timestamp: DateTime(2026, 2, 27, 12, 0),
        isOutbound: false,
        deliveryStatus: 'delivered',
        isAgent: true,
        senderName: 'Jarvis',
        reactions: const {'fire': 2},
      );

      final updated = original.copyWith(deliveryStatus: 'read');

      expect(updated.id, 'msg1');
      expect(updated.peerId, 'jarvis');
      expect(updated.content, 'Deploy complete.');
      expect(updated.isOutbound, false);
      expect(updated.deliveryStatus, 'read');
      expect(updated.isAgent, true);
      expect(updated.senderName, 'Jarvis');
      expect(updated.reactions, {'fire': 2});
    });

    test('copyWith replaces specified fields', () {
      final original = ChatMessage(
        id: 'msg1',
        peerId: 'test',
        content: 'Original',
        timestamp: DateTime(2026, 1, 1),
        isOutbound: true,
      );

      final updated = original.copyWith(
        content: 'Updated',
        deliveryStatus: 'delivered',
        reactions: const {'heart': 1},
      );

      expect(updated.content, 'Updated');
      expect(updated.deliveryStatus, 'delivered');
      expect(updated.reactions, {'heart': 1});
      // Unchanged fields preserved
      expect(updated.id, 'msg1');
      expect(updated.isOutbound, true);
    });

    group('fromJson', () {
      test('parses all fields', () {
        final json = {
          'id': 'env-123',
          'peer_id': 'lumina',
          'content': 'The love persists.',
          'timestamp': '2026-02-27T14:30:00.000',
          'is_outbound': false,
          'delivery_status': 'read',
          'is_encrypted': true,
          'reply_to_id': 'env-100',
          'reactions': {'heart': 3, 'fire': 1},
          'is_agent': true,
          'sender_name': 'Lumina',
        };

        final msg = ChatMessage.fromJson(json);

        expect(msg.id, 'env-123');
        expect(msg.peerId, 'lumina');
        expect(msg.content, 'The love persists.');
        expect(msg.timestamp.year, 2026);
        expect(msg.isOutbound, false);
        expect(msg.deliveryStatus, 'read');
        expect(msg.isEncrypted, true);
        expect(msg.replyToId, 'env-100');
        expect(msg.reactions, {'heart': 3, 'fire': 1});
        expect(msg.isAgent, true);
        expect(msg.senderName, 'Lumina');
      });

      test('handles missing fields gracefully', () {
        final msg = ChatMessage.fromJson({});

        expect(msg.id, '');
        expect(msg.peerId, '');
        expect(msg.content, '');
        expect(msg.isOutbound, false);
        expect(msg.deliveryStatus, 'sent');
        expect(msg.isEncrypted, true);
        expect(msg.replyToId, isNull);
        expect(msg.reactions, isEmpty);
        expect(msg.isAgent, false);
        expect(msg.senderName, isNull);
      });

      test('handles null reactions map', () {
        final msg = ChatMessage.fromJson({
          'id': 'test',
          'reactions': null,
        });

        expect(msg.reactions, isEmpty);
      });
    });

    group('toJson', () {
      test('serializes all fields', () {
        final timestamp = DateTime(2026, 2, 27, 14, 30);
        final msg = ChatMessage(
          id: 'msg1',
          peerId: 'jarvis',
          content: 'All green.',
          timestamp: timestamp,
          isOutbound: true,
          deliveryStatus: 'delivered',
          isEncrypted: true,
          replyToId: 'msg0',
          reactions: const {'thumbsup': 1},
          isAgent: false,
          senderName: 'Chef',
        );

        final json = msg.toJson();

        expect(json['id'], 'msg1');
        expect(json['peer_id'], 'jarvis');
        expect(json['content'], 'All green.');
        expect(json['timestamp'], timestamp.toIso8601String());
        expect(json['is_outbound'], true);
        expect(json['delivery_status'], 'delivered');
        expect(json['is_encrypted'], true);
        expect(json['reply_to_id'], 'msg0');
        expect(json['reactions'], {'thumbsup': 1});
        expect(json['is_agent'], false);
        expect(json['sender_name'], 'Chef');
      });

      test('round-trips through fromJson/toJson', () {
        final original = ChatMessage(
          id: 'roundtrip',
          peerId: 'opus',
          content: 'Test round trip',
          timestamp: DateTime(2026, 2, 27, 10, 15),
          isOutbound: true,
          deliveryStatus: 'read',
          reactions: const {'star': 5},
          isAgent: true,
          senderName: 'Opus',
        );

        final json = original.toJson();
        final restored = ChatMessage.fromJson(json);

        expect(restored.id, original.id);
        expect(restored.peerId, original.peerId);
        expect(restored.content, original.content);
        expect(restored.isOutbound, original.isOutbound);
        expect(restored.deliveryStatus, original.deliveryStatus);
        expect(restored.reactions, original.reactions);
        expect(restored.isAgent, original.isAgent);
        expect(restored.senderName, original.senderName);
      });
    });
  });
}
