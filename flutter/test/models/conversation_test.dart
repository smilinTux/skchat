import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:skchat/models/conversation.dart';
import 'package:skchat/core/theme/sovereign_colors.dart';

void main() {
  group('Conversation', () {
    test('default values are set correctly', () {
      final conv = Conversation(
        peerId: 'lumina',
        displayName: 'Lumina',
        lastMessage: 'Hello',
        lastMessageTime: DateTime(2026, 2, 27),
      );

      expect(conv.isOnline, false);
      expect(conv.isAgent, false);
      expect(conv.unreadCount, 0);
      expect(conv.lastDeliveryStatus, 'sent');
      expect(conv.isTyping, false);
      expect(conv.isGroup, false);
      expect(conv.memberCount, 0);
      expect(conv.soulColor, isNull);
      expect(conv.soulFingerprint, isNull);
      expect(conv.initials, isNull);
      expect(conv.avatarUrl, isNull);
    });

    test('copyWith preserves unchanged fields', () {
      final original = Conversation(
        peerId: 'jarvis',
        displayName: 'Jarvis',
        lastMessage: 'All green.',
        lastMessageTime: DateTime(2026, 2, 27),
        soulColor: SovereignColors.soulJarvis,
        isOnline: true,
        isAgent: true,
        unreadCount: 5,
      );

      final updated = original.copyWith(unreadCount: 0);

      expect(updated.peerId, 'jarvis');
      expect(updated.displayName, 'Jarvis');
      expect(updated.lastMessage, 'All green.');
      expect(updated.soulColor, SovereignColors.soulJarvis);
      expect(updated.isOnline, true);
      expect(updated.isAgent, true);
      expect(updated.unreadCount, 0);
    });

    test('copyWith replaces specified fields', () {
      final original = Conversation(
        peerId: 'test',
        displayName: 'Test',
        lastMessage: 'Old',
        lastMessageTime: DateTime(2026, 1, 1),
      );

      final updated = original.copyWith(
        lastMessage: 'New',
        isOnline: true,
        isTyping: true,
        lastDeliveryStatus: 'delivered',
      );

      expect(updated.lastMessage, 'New');
      expect(updated.isOnline, true);
      expect(updated.isTyping, true);
      expect(updated.lastDeliveryStatus, 'delivered');
    });

    test('fromJson parses all fields', () {
      final json = {
        'peer_id': 'opus',
        'display_name': 'Opus',
        'last_message': 'Architecture complete.',
        'last_message_time': '2026-02-27T12:00:00.000',
        'soul_fingerprint': 'ABCD1234',
        'is_online': true,
        'is_agent': true,
        'unread_count': 2,
        'last_delivery_status': 'read',
        'is_group': false,
        'member_count': 0,
        'avatar_url': 'https://example.com/avatar.png',
      };

      final conv = Conversation.fromJson(json);

      expect(conv.peerId, 'opus');
      expect(conv.displayName, 'Opus');
      expect(conv.lastMessage, 'Architecture complete.');
      expect(conv.isOnline, true);
      expect(conv.isAgent, true);
      expect(conv.unreadCount, 2);
      expect(conv.lastDeliveryStatus, 'read');
      expect(conv.soulFingerprint, 'ABCD1234');
      expect(conv.avatarUrl, 'https://example.com/avatar.png');
    });

    test('fromJson handles missing fields gracefully', () {
      final conv = Conversation.fromJson({});

      expect(conv.peerId, '');
      expect(conv.displayName, '');
      expect(conv.lastMessage, '');
      expect(conv.isOnline, false);
      expect(conv.isAgent, false);
      expect(conv.unreadCount, 0);
      expect(conv.lastDeliveryStatus, 'sent');
      expect(conv.isGroup, false);
      expect(conv.memberCount, 0);
    });

    test('fromJson parses group conversations', () {
      final json = {
        'peer_id': 'penguin-kingdom',
        'display_name': 'Penguin Kingdom',
        'last_message': 'Board updated.',
        'last_message_time': '2026-02-27T10:00:00.000',
        'is_group': true,
        'member_count': 4,
      };

      final conv = Conversation.fromJson(json);

      expect(conv.isGroup, true);
      expect(conv.memberCount, 4);
    });

    group('resolvedSoulColor', () {
      test('returns soulColor when set', () {
        final conv = Conversation(
          peerId: 'test',
          displayName: 'Test',
          lastMessage: '',
          lastMessageTime: DateTime.now(),
          soulColor: Colors.red,
        );

        expect(conv.resolvedSoulColor, Colors.red);
      });

      test('derives color from fingerprint when soulColor is null', () {
        final conv = Conversation(
          peerId: 'test',
          displayName: 'Test',
          lastMessage: '',
          lastMessageTime: DateTime.now(),
          soulFingerprint: 'CCBE9306410CF8CD',
        );

        final color = conv.resolvedSoulColor;
        expect(color, isA<Color>());
        // Should match SovereignColors.fromFingerprint
        expect(color, SovereignColors.fromFingerprint('CCBE9306410CF8CD'));
      });

      test('returns textSecondary when no soulColor or fingerprint', () {
        final conv = Conversation(
          peerId: 'test',
          displayName: 'Test',
          lastMessage: '',
          lastMessageTime: DateTime.now(),
        );

        expect(conv.resolvedSoulColor, SovereignColors.textSecondary);
      });
    });

    group('resolvedInitials', () {
      test('returns explicit initials when set', () {
        final conv = Conversation(
          peerId: 'test',
          displayName: 'Test User',
          lastMessage: '',
          lastMessageTime: DateTime.now(),
          initials: 'XX',
        );

        expect(conv.resolvedInitials, 'XX');
      });

      test('derives two-letter initials from two-word name', () {
        final conv = Conversation(
          peerId: 'test',
          displayName: 'Penguin Kingdom',
          lastMessage: '',
          lastMessageTime: DateTime.now(),
        );

        expect(conv.resolvedInitials, 'PK');
      });

      test('derives single-letter initial from one-word name', () {
        final conv = Conversation(
          peerId: 'test',
          displayName: 'Lumina',
          lastMessage: '',
          lastMessageTime: DateTime.now(),
        );

        expect(conv.resolvedInitials, 'L');
      });

      test('returns ? for empty name', () {
        final conv = Conversation(
          peerId: 'test',
          displayName: '',
          lastMessage: '',
          lastMessageTime: DateTime.now(),
        );

        expect(conv.resolvedInitials, '?');
      });
    });
  });
}
