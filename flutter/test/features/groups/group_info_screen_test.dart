import 'package:flutter_test/flutter_test.dart';
import 'package:skchat/features/groups/group_info_screen.dart';

void main() {
  group('GroupMemberInfo', () {
    test('defaults are correct', () {
      const member = GroupMemberInfo(
        identityUri: 'capauth://test',
        displayName: 'Test',
      );

      expect(member.identityUri, 'capauth://test');
      expect(member.displayName, 'Test');
      expect(member.role, MemberRole.member);
      expect(member.participantType, ParticipantType.human);
      expect(member.isOnline, false);
      expect(member.soulColor, isNull);
    });

    test('fromJson parses complete member data', () {
      final json = {
        'identity_uri': 'capauth://lumina',
        'display_name': 'Lumina',
        'role': 'admin',
        'participant_type': 'agent',
        'is_online': true,
      };

      final member = GroupMemberInfo.fromJson(json);

      expect(member.identityUri, 'capauth://lumina');
      expect(member.displayName, 'Lumina');
      expect(member.role, MemberRole.admin);
      expect(member.participantType, ParticipantType.agent);
      expect(member.isOnline, true);
    });

    test('fromJson handles missing fields with defaults', () {
      final member = GroupMemberInfo.fromJson({});

      expect(member.identityUri, '');
      expect(member.displayName, '');
      expect(member.role, MemberRole.member);
      expect(member.participantType, ParticipantType.human);
      expect(member.isOnline, false);
    });

    test('fromJson parses observer role', () {
      final json = {
        'identity_uri': 'capauth://viewer',
        'display_name': 'Viewer',
        'role': 'observer',
      };

      final member = GroupMemberInfo.fromJson(json);
      expect(member.role, MemberRole.observer);
    });

    test('fromJson parses service participant type', () {
      final json = {
        'identity_uri': 'capauth://bot',
        'display_name': 'Bot',
        'participant_type': 'service',
      };

      final member = GroupMemberInfo.fromJson(json);
      expect(member.participantType, ParticipantType.service);
    });

    test('fromJson defaults unknown role to member', () {
      final json = {
        'identity_uri': 'capauth://test',
        'display_name': 'Test',
        'role': 'unknown-role',
      };

      final member = GroupMemberInfo.fromJson(json);
      expect(member.role, MemberRole.member);
    });

    test('fromJson defaults unknown participant type to human', () {
      final json = {
        'identity_uri': 'capauth://test',
        'display_name': 'Test',
        'participant_type': 'cyborg',
      };

      final member = GroupMemberInfo.fromJson(json);
      expect(member.participantType, ParticipantType.human);
    });
  });

  group('MemberRole', () {
    test('has three values', () {
      expect(MemberRole.values.length, 3);
      expect(MemberRole.values, contains(MemberRole.admin));
      expect(MemberRole.values, contains(MemberRole.member));
      expect(MemberRole.values, contains(MemberRole.observer));
    });
  });

  group('ParticipantType', () {
    test('has three values', () {
      expect(ParticipantType.values.length, 3);
      expect(ParticipantType.values, contains(ParticipantType.human));
      expect(ParticipantType.values, contains(ParticipantType.agent));
      expect(ParticipantType.values, contains(ParticipantType.service));
    });
  });

  group('groupMembersProvider', () {
    test('provider is accessible', () {
      expect(groupMembersProvider, isNotNull);
    });
  });
}
