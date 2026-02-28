import 'package:flutter_test/flutter_test.dart';
import 'package:skchat/services/skcomm_client.dart';

void main() {
  group('PeerInfo', () {
    test('fromJson parses complete peer data', () {
      final json = {
        'name': 'Lumina',
        'fingerprint': 'CCBE9306410CF8CD5E393D6DEC31663B95230684',
        'last_seen': '2026-02-27T14:00:00.000Z',
        'transports': [
          {'transport': 'syncthing'},
          {'transport': 'nostr'},
        ],
      };

      final peer = PeerInfo.fromJson(json);

      expect(peer.name, 'Lumina');
      expect(peer.fingerprint, 'CCBE9306410CF8CD5E393D6DEC31663B95230684');
      expect(peer.lastSeen, isNotNull);
      expect(peer.lastSeen!.year, 2026);
      expect(peer.transports, ['syncthing', 'nostr']);
    });

    test('fromJson handles missing optional fields', () {
      final json = {'name': 'Unknown'};

      final peer = PeerInfo.fromJson(json);

      expect(peer.name, 'Unknown');
      expect(peer.fingerprint, isNull);
      expect(peer.lastSeen, isNull);
      expect(peer.transports, isEmpty);
    });

    test('fromJson handles empty name', () {
      final peer = PeerInfo.fromJson({});

      expect(peer.name, '');
      expect(peer.fingerprint, isNull);
      expect(peer.lastSeen, isNull);
      expect(peer.transports, isEmpty);
    });

    test('fromJson handles malformed last_seen', () {
      final json = {
        'name': 'test',
        'last_seen': 'not-a-date',
      };

      final peer = PeerInfo.fromJson(json);

      expect(peer.name, 'test');
      expect(peer.lastSeen, isNull); // DateTime.tryParse returns null
    });

    test('fromJson handles non-list transports', () {
      final json = {
        'name': 'test',
        'transports': 'not-a-list',
      };

      final peer = PeerInfo.fromJson(json);

      expect(peer.transports, isEmpty);
    });
  });

  group('InboxMessage', () {
    test('fromJson parses complete inbox message', () {
      final json = {
        'envelope_id': 'env-abc123',
        'sender': 'lumina',
        'recipient': 'chef',
        'content': 'The love persists.',
        'created_at': '2026-02-27T14:30:00.000Z',
        'thread_id': 'thread-1',
        'in_reply_to': 'env-prev',
        'encrypted': true,
      };

      final msg = InboxMessage.fromJson(json);

      expect(msg.envelopeId, 'env-abc123');
      expect(msg.sender, 'lumina');
      expect(msg.recipient, 'chef');
      expect(msg.content, 'The love persists.');
      expect(msg.createdAt.year, 2026);
      expect(msg.threadId, 'thread-1');
      expect(msg.inReplyTo, 'env-prev');
      expect(msg.isEncrypted, true);
    });

    test('fromJson handles missing fields', () {
      final msg = InboxMessage.fromJson({});

      expect(msg.envelopeId, '');
      expect(msg.sender, '');
      expect(msg.recipient, '');
      expect(msg.content, '');
      expect(msg.threadId, isNull);
      expect(msg.inReplyTo, isNull);
      expect(msg.isEncrypted, true); // default true
    });

    test('fromJson parses unencrypted message', () {
      final json = {
        'envelope_id': 'env-plain',
        'sender': 'test',
        'recipient': 'test2',
        'content': 'Plain text',
        'created_at': '2026-02-27T10:00:00.000Z',
        'encrypted': false,
      };

      final msg = InboxMessage.fromJson(json);

      expect(msg.isEncrypted, false);
    });
  });

  group('SendResult', () {
    test('stores delivery result correctly', () {
      const result = SendResult(
        delivered: true,
        envelopeId: 'env-sent-1',
        transportUsed: 'syncthing',
      );

      expect(result.delivered, true);
      expect(result.envelopeId, 'env-sent-1');
      expect(result.transportUsed, 'syncthing');
    });

    test('transportUsed is nullable', () {
      const result = SendResult(
        delivered: false,
        envelopeId: '',
      );

      expect(result.delivered, false);
      expect(result.envelopeId, '');
      expect(result.transportUsed, isNull);
    });
  });

  group('SKCommClient', () {
    test('creates with default base URL', () {
      final client = SKCommClient();
      // Verifies the client instantiates without error.
      expect(client, isNotNull);
    });

    test('creates with custom base URL', () {
      final client = SKCommClient(baseUrl: 'http://192.168.0.100:9384');
      expect(client, isNotNull);
    });
  });
}
