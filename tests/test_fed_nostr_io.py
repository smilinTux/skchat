from skchat.spaces.federation.events import (
    FOCUS_KIND,
    MEMBERSHIP_KIND,
    SPACE_KIND,
    build_membership,
)
from skchat.spaces.federation.focus import Membership
from skchat.spaces.federation.nostr_io import FederationNostr


class _Recorder:
    def __init__(self, query_result=None):
        self.published = []
        self.queried = []
        self._query_result = query_result or []

    def publish(self, event):
        self.published.append(event)
        return True

    def query(self, filters):
        self.queried.append(filters)
        return list(self._query_result)


def test_publish_focus_uses_focus_kind():
    rec = _Recorder()
    fn = FederationNostr(publish=rec.publish, query=rec.query)
    ok = fn.publish_focus(host_fqid="lumina@chef.skworld",
                          auth_url="https://h/sfu/get", sfu_ws_url="wss://h:8443")
    assert ok is True
    assert len(rec.published) == 1
    assert rec.published[0]["kind"] == FOCUS_KIND


def test_publish_space_uses_space_kind():
    rec = _Recorder()
    fn = FederationNostr(publish=rec.publish, query=rec.query)
    fn.publish_space(space_id="space-x", title="Town Hall",
                     host_fqid="lumina@chef.skworld", status="live")
    assert rec.published[0]["kind"] == SPACE_KIND


def test_publish_membership_uses_membership_kind():
    rec = _Recorder()
    fn = FederationNostr(publish=rec.publish, query=rec.query)
    fn.publish_membership(fqid="opus@chef.skworld", space_id="space-x",
                          foci_preferred="lumina@chef.skworld", issued_at=123)
    assert rec.published[0]["kind"] == MEMBERSHIP_KIND


def test_query_memberships_filters_kind_and_space_and_parses():
    ev1 = build_membership(fqid="a@h", space_id="space-x",
                           foci_preferred="sfu-a", issued_at=100)
    ev2 = build_membership(fqid="b@h", space_id="space-x",
                           foci_preferred="sfu-b", issued_at=200)
    rec = _Recorder(query_result=[ev1, ev2])
    fn = FederationNostr(publish=rec.publish, query=rec.query)
    members = fn.query_memberships("space-x")
    # the filter targets the membership kind + this space's `a` tag
    flt = rec.queried[0]
    assert MEMBERSHIP_KIND in flt["kinds"]
    assert flt["#a"] == [f"{SPACE_KIND}:space-x"]
    # results parse into Membership objects
    assert all(isinstance(m, Membership) for m in members)
    assert {m.fqid for m in members} == {"a@h", "b@h"}
    assert {m.foci_preferred for m in members} == {"sfu-a", "sfu-b"}
