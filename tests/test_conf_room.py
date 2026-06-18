"""Tests for the multi-party video conference model + lifecycle (skchat.conf)."""

from skchat.conf import Conf, ConfRegistry, ConfStatus, derive_conf_id
from skchat.spaces.space import SpaceStatus


# --- derive_conf_id -----------------------------------------------------------
def test_named_id_is_deterministic_for_same_host_and_slug():
    a = derive_conf_id("lumina@chef.skworld", "standup")
    b = derive_conf_id("lumina@chef.skworld", "standup")
    assert a == b
    assert a.startswith("conf-")
    assert len(a) == len("conf-") + 16


def test_named_id_is_whitespace_insensitive():
    assert derive_conf_id(" lumina@chef.skworld ", " standup ") == derive_conf_id(
        "lumina@chef.skworld", "standup"
    )


def test_named_id_differs_by_slug_and_host():
    base = derive_conf_id("lumina@chef.skworld", "standup")
    assert derive_conf_id("lumina@chef.skworld", "retro") != base
    assert derive_conf_id("opus@chef.skworld", "standup") != base


def test_adhoc_id_is_random_each_call():
    ids = {derive_conf_id("lumina@chef.skworld") for _ in range(50)}
    assert len(ids) == 50  # every ad-hoc "new meeting" room is unique
    assert all(i.startswith("conf-") and len(i) == len("conf-") + 16 for i in ids)


# --- Conf dataclass -----------------------------------------------------------
def test_conf_defaults_and_room_property():
    c = Conf(conf_id="conf-abc", host_fqid="lumina@chef.skworld", title="All Hands")
    assert c.status is SpaceStatus.OPEN
    assert c.participant_cap == 20
    assert c.room == "conf-abc"


def test_conf_status_is_spaces_status():
    assert ConfStatus is SpaceStatus


# --- ConfRegistry lifecycle ---------------------------------------------------
def test_create_get_list_live_end_lifecycle(tmp_path):
    reg = ConfRegistry(path=tmp_path / "confs.json")
    conf = reg.create("lumina@chef.skworld", "Sprint Planning", slug="sprint")

    assert reg.get(conf.conf_id) is conf
    assert conf.status is SpaceStatus.OPEN
    assert conf.participant_cap == 20
    assert reg.list_live() == [conf]

    reg.end(conf.conf_id)
    assert reg.get(conf.conf_id).status is SpaceStatus.ENDED
    assert reg.list_live() == []


def test_create_named_room_is_stable_across_registries(tmp_path):
    p = tmp_path / "confs.json"
    first = ConfRegistry(path=p).create("lumina@chef.skworld", "Retro", slug="retro")
    # Same (host, slug) → same id even from a fresh registry.
    again = ConfRegistry(path=p).create("lumina@chef.skworld", "Retro", slug="retro")
    assert first.conf_id == again.conf_id


def test_create_adhoc_rooms_differ(tmp_path):
    reg = ConfRegistry(path=tmp_path / "confs.json")
    a = reg.create("lumina@chef.skworld", "Quick Sync")
    b = reg.create("lumina@chef.skworld", "Quick Sync")
    assert a.conf_id != b.conf_id
    assert len(reg.list_live()) == 2


def test_custom_participant_cap(tmp_path):
    reg = ConfRegistry(path=tmp_path / "confs.json")
    c = reg.create("lumina@chef.skworld", "Town Hall", slug="th", participant_cap=100)
    assert c.participant_cap == 100


def test_persists_across_instances(tmp_path):
    p = tmp_path / "confs.json"
    c = ConfRegistry(path=p).create("lumina@chef.skworld", "Demo", slug="demo")
    reloaded = ConfRegistry(path=p)
    got = reloaded.get(c.conf_id)
    assert got is not None
    assert got.title == "Demo"
    assert got.status is SpaceStatus.OPEN
    assert reloaded.list_live() == [got]


def test_get_unknown_returns_none(tmp_path):
    assert ConfRegistry(path=tmp_path / "confs.json").get("nope") is None
