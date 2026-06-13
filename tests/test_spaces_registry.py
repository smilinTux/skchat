from skchat.spaces.registry import SpaceRegistry
from skchat.spaces.space import Space, SpaceStatus


def _space(sid="space-aaaa1111aaaa1111"):
    return Space(space_id=sid, host_fqid="lumina@chef.skworld",
                 title="Town Hall", slug="town-hall")


def test_register_and_list_live(tmp_path):
    reg = SpaceRegistry(path=tmp_path / "spaces.json")
    reg.add(_space())
    live = reg.live()
    assert len(live) == 1
    assert live[0].space_id == "space-aaaa1111aaaa1111"


def test_end_removes_from_live(tmp_path):
    reg = SpaceRegistry(path=tmp_path / "spaces.json")
    s = _space()
    reg.add(s)
    reg.end(s.space_id)
    assert reg.live() == []
    assert reg.get(s.space_id).status == SpaceStatus.ENDED


def test_persists_across_instances(tmp_path):
    p = tmp_path / "spaces.json"
    SpaceRegistry(path=p).add(_space())
    reloaded = SpaceRegistry(path=p)
    assert len(reloaded.live()) == 1


def test_get_unknown_returns_none(tmp_path):
    assert SpaceRegistry(path=tmp_path / "spaces.json").get("nope") is None
