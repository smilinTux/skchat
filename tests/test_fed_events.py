from skchat.spaces.federation.events import (
    FOCUS_KIND,
    MEMBERSHIP_KIND,
    SPACE_KIND,
    build_focus_descriptor,
    build_membership,
    build_space_state,
    parse_focus_descriptor,
    parse_membership,
)


def test_focus_descriptor_roundtrip():
    ev = build_focus_descriptor(
        host_fqid="lumina@chef.skworld", auth_url="https://h/sfu/get", sfu_ws_url="wss://h:8443"
    )
    assert ev["kind"] == FOCUS_KIND
    d = parse_focus_descriptor(ev)
    assert d["host_fqid"] == "lumina@chef.skworld"
    assert d["auth_url"] == "https://h/sfu/get"
    assert d["sfu_ws_url"] == "wss://h:8443"


def test_membership_roundtrip_carries_foci_preferred():
    ev = build_membership(
        fqid="opus@chef.skworld",
        space_id="space-x",
        foci_preferred="lumina@chef.skworld",
        issued_at=123,
    )
    assert ev["kind"] == MEMBERSHIP_KIND
    m = parse_membership(ev)
    assert m.fqid == "opus@chef.skworld"
    assert m.foci_preferred == "lumina@chef.skworld"
    assert m.issued_at == 123


def test_parse_membership_tolerates_none_tags():
    m = parse_membership({"tags": None})
    assert m.fqid == ""
    assert m.foci_preferred == ""
    assert m.issued_at == 0


def test_parse_membership_skips_non_list_tag_entries():
    m = parse_membership({"tags": [123, "x", ["fqid", "a@h"]]})
    assert m.fqid == "a@h"


def test_parse_membership_coerces_bad_created_at():
    m = parse_membership({"created_at": "notint", "tags": []})
    assert m.issued_at == 0


def test_parse_focus_descriptor_tolerates_bad_json():
    assert parse_focus_descriptor({"content": "not json"}) == {}


def test_space_state_has_kind_and_title():
    ev = build_space_state(
        space_id="space-x", title="Town Hall", host_fqid="lumina@chef.skworld", status="live"
    )
    assert ev["kind"] == SPACE_KIND
    assert any(t == ["title", "Town Hall"] for t in ev["tags"])
