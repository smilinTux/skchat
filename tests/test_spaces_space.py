from skchat.spaces.space import Space, SpaceStatus, derive_space_id


def test_space_id_is_deterministic_and_prefixed():
    a = derive_space_id("lumina@chef.skworld", "town-hall")
    b = derive_space_id("lumina@chef.skworld", "town-hall")
    assert a == b
    assert a.startswith("space-")
    # 16 base32 chars after the prefix
    assert len(a) == len("space-") + 16
    assert a[len("space-"):].isalnum()


def test_space_id_varies_by_host_and_slug():
    assert derive_space_id("lumina@chef.skworld", "town-hall") != \
        derive_space_id("opus@chef.skworld", "town-hall")
    assert derive_space_id("lumina@chef.skworld", "town-hall") != \
        derive_space_id("lumina@chef.skworld", "after-party")


def test_space_dataclass_defaults_and_room_equals_id():
    s = Space(space_id="space-abcd1234abcd1234", host_fqid="lumina@chef.skworld",
              title="Town Hall", slug="town-hall")
    assert s.status == SpaceStatus.OPEN
    assert s.room == s.space_id          # the LiveKit room name IS the space id
    assert s.speaker_cap == 10           # configurable default (spec §1 / Chef)
