import pytest

from skchat.call_session import (
    CALL_INVITE_SUBJECT,
    build_invite_body,
    derive_room,
    parse_invite_body,
)


def test_derive_room_is_order_independent():
    a, b = "lumina@chef.skworld", "opus@chef.skworld"
    assert derive_room(a, b) == derive_room(b, a)


def test_derive_room_is_stable_and_well_formed():
    room = derive_room("lumina@chef.skworld", "opus@chef.skworld")
    assert room.startswith("call-")
    assert room == derive_room("lumina@chef.skworld", "opus@chef.skworld")
    assert "lumina" not in room and "opus" not in room
    suffix = room[len("call-") :]
    assert len(suffix) == 16 and suffix == suffix.lower()


def test_derive_room_distinct_pairs_differ():
    assert derive_room("a@x.y", "b@x.y") != derive_room("a@x.y", "c@x.y")


def test_derive_room_strips_whitespace():
    assert derive_room("  lumina@chef.skworld  ", "opus@chef.skworld") == derive_room(
        "lumina@chef.skworld", "opus@chef.skworld"
    )


def test_invite_body_roundtrip():
    body = build_invite_body(
        from_fqid="opus@chef.skworld",
        to_fqid="lumina@chef.skworld",
        room="call-abc",
        livekit_url="wss://noroc2027.tail204f0c.ts.net:8443",
    )
    inv = parse_invite_body(body)
    assert inv["type"] == CALL_INVITE_SUBJECT
    assert inv["from_fqid"] == "opus@chef.skworld"
    assert inv["to_fqid"] == "lumina@chef.skworld"
    assert inv["room"] == "call-abc"
    assert inv["transport"] == "livekit"
    assert "nonce" in inv and "ts" in inv


def test_parse_invite_rejects_non_invite():
    with pytest.raises(ValueError):
        parse_invite_body('{"type":"SOMETHING_ELSE"}')


# ── QA Area 3: call_session edge cases ───────────────────────────────────────


def test_parse_invite_rejects_non_json():
    with pytest.raises(ValueError, match="invalid JSON"):
        parse_invite_body("{not json at all")


def test_parse_invite_rejects_missing_type():
    # a JSON object with no "type" key is not a CALL_INVITE
    with pytest.raises(ValueError, match="not a CALL_INVITE"):
        parse_invite_body('{"from_fqid":"a@b","room":"call-x"}')


def test_each_invite_gets_a_unique_nonce():
    b1 = build_invite_body(from_fqid="a@b", to_fqid="c@d", room="call-x", livekit_url="wss://h")
    b2 = build_invite_body(from_fqid="a@b", to_fqid="c@d", room="call-x", livekit_url="wss://h")
    assert parse_invite_body(b1)["nonce"] != parse_invite_body(b2)["nonce"]


def test_invite_carries_topic_when_supplied():
    body = build_invite_body(
        from_fqid="a@b",
        to_fqid="c@d",
        room="call-x",
        livekit_url="wss://h",
        topic="ingest debugging",
    )
    assert parse_invite_body(body)["topic"] == "ingest debugging"


def test_invite_topic_defaults_empty():
    body = build_invite_body(from_fqid="a@b", to_fqid="c@d", room="call-x", livekit_url="wss://h")
    assert parse_invite_body(body)["topic"] == ""


def test_self_pair_room_is_deterministic():
    # degenerate self-call: deterministic + well-formed (no crash)
    room = derive_room("solo@chef.skworld", "solo@chef.skworld")
    assert room.startswith("call-") and len(room[len("call-") :]) == 16
