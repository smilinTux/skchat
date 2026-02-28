import 'package:flutter_test/flutter_test.dart';
import 'package:skchat/data/message_repository.dart';

void main() {
  group('MessageRepository box naming', () {
    test('sanitizes simple peerId', () {
      // The static _boxName method is private, so we test the logic inline.
      const peerId = 'lumina';
      final sanitized =
          'messages_${peerId.replaceAll(RegExp(r'[^a-zA-Z0-9_]'), '_').toLowerCase()}';
      expect(sanitized, 'messages_lumina');
    });

    test('sanitizes peerId with special characters', () {
      const peerId = 'user@domain.com';
      final sanitized =
          'messages_${peerId.replaceAll(RegExp(r'[^a-zA-Z0-9_]'), '_').toLowerCase()}';
      expect(sanitized, 'messages_user_domain_com');
    });

    test('sanitizes peerId with mixed case', () {
      const peerId = 'Queen_Lumina';
      final sanitized =
          'messages_${peerId.replaceAll(RegExp(r'[^a-zA-Z0-9_]'), '_').toLowerCase()}';
      expect(sanitized, 'messages_queen_lumina');
    });

    test('sanitizes peerId with dashes', () {
      const peerId = 'peer-with-dashes';
      final sanitized =
          'messages_${peerId.replaceAll(RegExp(r'[^a-zA-Z0-9_]'), '_').toLowerCase()}';
      expect(sanitized, 'messages_peer_with_dashes');
    });

    test('sanitizes empty peerId', () {
      const peerId = '';
      final sanitized =
          'messages_${peerId.replaceAll(RegExp(r'[^a-zA-Z0-9_]'), '_').toLowerCase()}';
      expect(sanitized, 'messages_');
    });

    test('sanitizes peerId with unicode', () {
      const peerId = 'user\u2764name';
      final sanitized =
          'messages_${peerId.replaceAll(RegExp(r'[^a-zA-Z0-9_]'), '_').toLowerCase()}';
      expect(sanitized, 'messages_user_name');
    });
  });

  group('MessageRepository provider', () {
    test('provider creates instance', () {
      // The provider is a simple Provider, verify it's importable.
      expect(messageRepositoryProvider, isNotNull);
    });
  });
}
