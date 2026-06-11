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
