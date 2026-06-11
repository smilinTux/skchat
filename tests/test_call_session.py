from skchat.call_session import derive_room


def test_derive_room_is_order_independent():
    a, b = "lumina@chef.skworld", "opus@chef.skworld"
    assert derive_room(a, b) == derive_room(b, a)


def test_derive_room_is_stable_and_well_formed():
    room = derive_room("lumina@chef.skworld", "opus@chef.skworld")
    assert room.startswith("call-")
    assert room == derive_room("lumina@chef.skworld", "opus@chef.skworld")
    assert "lumina" not in room and "opus" not in room
    suffix = room[len("call-"):]
    assert len(suffix) == 16 and suffix == suffix.lower()


def test_derive_room_distinct_pairs_differ():
    assert derive_room("a@x.y", "b@x.y") != derive_room("a@x.y", "c@x.y")


from skchat.call_session import (
    CALL_INVITE_SUBJECT,
    build_invite_body,
    parse_invite_body,
)


def test_invite_body_roundtrip():
    body = build_invite_body(
        from_fqid="opus@chef.skworld",
        to_fqid="lumina@chef.skworld",
        room="call-abc",
        livekit_url="wss://noroc2027.tail204f0c.ts.net:8443",
    )
    inv = parse_invite_body(body)
    assert inv["type"] == "CALL_INVITE"
    assert inv["from_fqid"] == "opus@chef.skworld"
    assert inv["to_fqid"] == "lumina@chef.skworld"
    assert inv["room"] == "call-abc"
    assert inv["transport"] == "livekit"
    assert "nonce" in inv and "ts" in inv


def test_parse_invite_rejects_non_invite():
    import pytest
    with pytest.raises(ValueError):
        parse_invite_body('{"type":"SOMETHING_ELSE"}')
