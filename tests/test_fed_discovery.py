"""Unit tests for the federation discovery client (U8).

All seams are faked — no live Nostr relays, no SFU HTTP, no capauth signing.
"""

import pytest

from skchat.spaces.federation.discovery import (
    AuthDenied,
    DiscoveryError,
    ElectedHost,
    FederationDiscoveryClient,
)
from skchat.spaces.federation.events import build_focus_descriptor, build_membership
from skchat.spaces.federation.nostr_io import FederationNostr


class _FakeRelay:
    """A fake relay query seam: returns canned events keyed loosely by filter."""

    def __init__(self, membership_events=None, focus_events=None):
        self._memberships = membership_events or []
        self._focus = focus_events or []
        self.queries = []

    def query(self, filters):
        self.queries.append(filters)
        kinds = filters.get("kinds") or []
        # focus descriptors vs membership presence are distinguished by kind
        from skchat.spaces.federation.events import FOCUS_KIND, MEMBERSHIP_KIND

        if FOCUS_KIND in kinds:
            return list(self._focus)
        if MEMBERSHIP_KIND in kinds:
            return list(self._memberships)
        return []


class _FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _nostr(relay):
    # inject the fake query seam; publish never used here
    return FederationNostr(publish=lambda ev: True, query=relay.query)


def test_discover_and_elect_oldest_host_wins():
    # two members advertise different foci; oldest issued_at wins (focus.py rule)
    memberships = [
        build_membership(
            fqid="b@chef.skworld",
            space_id="space-x",
            foci_preferred="lumina@chef.skworld",
            issued_at=200,
        ),
        build_membership(
            fqid="a@chef.skworld",
            space_id="space-x",
            foci_preferred="opus@chef.skworld",
            issued_at=100,  # oldest -> its preferred focus wins
        ),
    ]
    focus = [
        build_focus_descriptor(
            host_fqid="opus@chef.skworld",
            auth_url="https://opus.skworld/sfu/get",
            sfu_ws_url="wss://opus.skworld:8443",
        ),
        build_focus_descriptor(
            host_fqid="lumina@chef.skworld",
            auth_url="https://lumina.skworld/sfu/get",
            sfu_ws_url="wss://lumina.skworld:8443",
        ),
    ]
    relay = _FakeRelay(membership_events=memberships, focus_events=focus)
    client = FederationDiscoveryClient(nostr=_nostr(relay))
    host = client.discover_and_elect("space-x")
    assert isinstance(host, ElectedHost)
    assert host.fqid == "opus@chef.skworld"
    assert host.auth_url == "https://opus.skworld/sfu/get"
    assert host.sfu_ws_url == "wss://opus.skworld:8443"


def test_discover_skips_malformed_membership_events():
    good = build_membership(
        fqid="a@chef.skworld",
        space_id="space-x",
        foci_preferred="lumina@chef.skworld",
        issued_at=100,
    )
    bad = "not even an event"  # would crash a naive parser
    focus = [
        build_focus_descriptor(
            host_fqid="lumina@chef.skworld",
            auth_url="https://lumina.skworld/sfu/get",
            sfu_ws_url="wss://lumina.skworld:8443",
        )
    ]
    relay = _FakeRelay(membership_events=[bad, good], focus_events=focus)
    client = FederationDiscoveryClient(nostr=_nostr(relay))
    host = client.discover_and_elect("space-x")
    assert host.fqid == "lumina@chef.skworld"


def test_discover_no_members_raises_discovery_error():
    relay = _FakeRelay(membership_events=[], focus_events=[])
    client = FederationDiscoveryClient(nostr=_nostr(relay))
    with pytest.raises(DiscoveryError):
        client.discover_and_elect("space-x")


def test_discover_elected_host_without_descriptor_raises():
    # membership elects a focus that has NO advertised descriptor
    memberships = [
        build_membership(
            fqid="a@chef.skworld",
            space_id="space-x",
            foci_preferred="ghost@chef.skworld",
            issued_at=100,
        )
    ]
    relay = _FakeRelay(membership_events=memberships, focus_events=[])
    client = FederationDiscoveryClient(nostr=_nostr(relay))
    with pytest.raises(DiscoveryError):
        client.discover_and_elect("space-x")


def test_build_signed_assertion_uses_injected_sign():
    relay = _FakeRelay()
    client = FederationDiscoveryClient(nostr=_nostr(relay), sign=lambda payload: "SIG")
    signed = client.build_signed_assertion(fqid="a@chef.skworld", space_id="space-x")
    assert signed["sig"] == "SIG"
    import json

    claim = json.loads(signed["claim"])
    assert claim["fqid"] == "a@chef.skworld"
    assert claim["space_id"] == "space-x"
    assert claim["nonce"]  # a fresh nonce was minted


def test_build_signed_assertion_mints_fresh_nonce_each_call():
    client = FederationDiscoveryClient(nostr=_nostr(_FakeRelay()), sign=lambda p: "SIG")
    import json

    n1 = json.loads(client.build_signed_assertion(fqid="a@h", space_id="s")["claim"])["nonce"]
    n2 = json.loads(client.build_signed_assertion(fqid="a@h", space_id="s")["claim"])["nonce"]
    assert n1 != n2


def test_get_token_happy_path():
    host = ElectedHost(
        fqid="opus@chef.skworld",
        auth_url="https://opus.skworld/sfu/get",
        sfu_ws_url="wss://opus.skworld:8443",
    )
    posted = {}

    def fake_post(url, body):
        posted["url"] = url
        posted["body"] = body
        return _FakeResp(200, {"token": "JWT", "role": "speaker", "sfu_ws_url": host.sfu_ws_url})

    client = FederationDiscoveryClient(
        nostr=_nostr(_FakeRelay()), post=fake_post, sign=lambda p: "SIG"
    )
    out = client.get_token(host, fqid="a@chef.skworld", space_id="space-x")
    assert out["token"] == "JWT"
    assert out["role"] == "speaker"
    # the signed assertion was posted to the elected host's auth_url
    assert posted["url"] == "https://opus.skworld/sfu/get"
    assert posted["body"]["sig"] == "SIG"
    assert "claim" in posted["body"]


def test_get_token_403_raises_auth_denied():
    host = ElectedHost(
        fqid="opus@chef.skworld",
        auth_url="https://opus.skworld/sfu/get",
        sfu_ws_url="wss://opus.skworld:8443",
    )
    client = FederationDiscoveryClient(
        nostr=_nostr(_FakeRelay()),
        post=lambda url, body: _FakeResp(403, {"detail": "fqid not permitted"}),
        sign=lambda p: "SIG",
    )
    with pytest.raises(AuthDenied):
        client.get_token(host, fqid="evil@attacker", space_id="space-x")


def test_get_token_non_2xx_raises_discovery_error():
    host = ElectedHost(
        fqid="opus@chef.skworld",
        auth_url="https://opus.skworld/sfu/get",
        sfu_ws_url="wss://opus.skworld:8443",
    )
    client = FederationDiscoveryClient(
        nostr=_nostr(_FakeRelay()),
        post=lambda url, body: _FakeResp(500),
        sign=lambda p: "SIG",
    )
    with pytest.raises(DiscoveryError):
        client.get_token(host, fqid="a@chef.skworld", space_id="space-x")
