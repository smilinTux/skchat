"""A room built by "New guest group" is a guest room, and must render as one.

Only ONE server path ever set ``mode="gdm"``: promoting an existing DM. The
"New guest group" flow instead creates a PLAIN group and mints group invites
against it, so a room built specifically to hold guests carried no mode at all
and fell outside every guest surface: no guest badge, absent from the Guests
filter, and exempt from whole-group expiry, while being full of untrusted
people.

Observed live before the fix: a room with one guest seated reported
``mode=None``, and the operator could not find it under Guests.

These pin the rule that replaced it: a group is guest-family because of who is
SEATED in it, not because of how it happened to be created.
"""

from __future__ import annotations

from skchat import daemon_proxy_groups as G
from skchat import guest_groups as GG
from skchat.group import GroupMember  # noqa: E402


def _Member(uri, name):
    """Real GroupMember: group_to_conversation reads enum .value off role and
    participant_type, so a string stub would pass here and lie."""
    return GroupMember(identity_uri=uri, display_name=name)


class _Group:
    """Minimal stand-in with the surface these paths touch."""

    def __init__(self, metadata, members=()):
        self.id = "g-1"
        self.name = "test2"
        self.description = ""
        self.metadata = metadata
        self.members = list(members)
        self.member_count = len(self.members)
        from datetime import datetime, timezone

        self.updated_at = datetime.now(timezone.utc)

    def get_member(self, uri):
        return next((m for m in self.members if m.identity_uri == uri), None)


def _guest_room(**extra_meta):
    """A room as "New guest group" leaves it: NO mode, one guest seated."""
    md = {"guests": {"guest:guest#abc": {"joined_at": 1.0}}}
    md.update(extra_meta)
    return _Group(
        md, [_Member("capauth:lumina@skworld.io", "Lumina"), _Member("guest:guest#abc", "Guest")]
    )


class TestSeatedGuestsMakeItRenderAsAGuestRoom:
    def test_an_untagged_room_with_a_guest_has_seated_guests(self):
        assert GG.has_seated_guests(_guest_room()) is True

    def test_a_plain_group_with_no_guests_does_not(self):
        """The rule must not swallow ordinary groups: an agent-only room stays
        an ordinary room."""
        plain = _Group({}, [_Member("capauth:lumina@skworld.io", "Lumina")])
        assert GG.has_seated_guests(plain) is False

    def test_none_is_handled(self):
        assert GG.has_seated_guests(None) is False

    def test_an_empty_guests_map_does_not_count(self):
        """A room whose guests have all left is not a guest room again."""
        assert GG.has_seated_guests(_Group({"guests": {}})) is False


class TestItRendersAsAGuestRoom:
    def test_the_payload_reports_gdm_so_the_guests_filter_catches_it(self, monkeypatch):
        monkeypatch.setattr(GG, "guest_ring_ts", lambda fp: None, raising=False)
        monkeypatch.setattr(GG, "get_dm_contact_by_fp", lambda fp: None, raising=False)
        conv = G.group_to_conversation(_guest_room())
        # This is the field the Guests filter and the badge both key off.
        assert conv["mode"] == "gdm"

    def test_an_ordinary_group_payload_is_untouched(self, monkeypatch):
        plain = _Group({}, [_Member("capauth:lumina@skworld.io", "Lumina")])
        conv = G.group_to_conversation(plain)
        assert "mode" not in conv
        assert conv.get("guest_dm") is None

    def test_members_carry_guest_fields_so_the_roster_can_style_them(self, monkeypatch):
        monkeypatch.setattr(GG, "guest_ring_ts", lambda fp: None, raising=False)
        conv = G.group_to_conversation(_guest_room())
        seats = {p.get("identity_uri"): p for p in conv["participants"]}
        assert seats["guest:guest#abc"].get("guest") is True


class TestEnforcementSemanticsAreDELIBERATELYUnchanged:
    """The first cut of this fix widened is_guest_dm_like to include seated
    guests. That broke four documented behaviours at once, most sharply
    history fencing: a classic guest group's guests are MEANT to see pre-join
    history, and dm-family guests are fenced from it. Silently fencing rooms
    that already exist would change what guests can see.

    So the fix is render-only. These pin that, and the missing whole-group
    expiry on classic guest groups stays a known gap for Chef to rule on, not
    something slipped in under a rendering change."""

    def test_a_guest_bearing_plain_group_is_still_not_dm_family(self):
        assert GG.is_guest_dm_like(_guest_room()) is False

    def test_seat_cap_semantics_are_untouched(self):
        assert GG.guest_seat_cap(_Group({"mode": "dm"})) == GG.DM_SEAT_CAP
        assert GG.guest_seat_cap(_Group({"mode": "gdm", "seat_cap": 7})) == 7

    def test_the_read_side_helper_still_sees_the_guests(self):
        """has_seated_guests is what the payload uses; it must stay true even
        though the dm-family test above is false."""
        assert GG.has_seated_guests(_guest_room()) is True
        assert GG.has_seated_guests(_Group({})) is False
