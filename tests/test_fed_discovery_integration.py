"""Integration tests for GET /sfu/candidates (U8) via FastAPI TestClient.

The Nostr relay layer is monkeypatched (skcomms.transports.nostr._query_relay)
so no live relay is touched.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.spaces.federation.events import build_focus_descriptor
from skchat.spaces.registry import SpaceRegistry
from skchat.spaces.routes import register_spaces_routes


@pytest.fixture
def app_client(tmp_path):
    app = FastAPI()
    register_spaces_routes(app, registry=SpaceRegistry(path=tmp_path / "spaces.json"))
    return TestClient(app)


def test_candidates_empty_when_no_relays_configured(app_client, monkeypatch):
    monkeypatch.delenv("SKCHAT_NOSTR_RELAYS", raising=False)
    r = app_client.get("/sfu/candidates")
    assert r.status_code == 200
    assert r.json() == {"hosts": []}


def test_candidates_lists_advertised_focus_hosts(app_client, monkeypatch):
    descriptors = [
        build_focus_descriptor(
            host_fqid="lumina@chef.skworld",
            auth_url="https://lumina.skworld/sfu/get",
            sfu_ws_url="wss://lumina.skworld:8443",
        ),
        build_focus_descriptor(
            host_fqid="opus@chef.skworld",
            auth_url="https://opus.skworld/sfu/get",
            sfu_ws_url="wss://opus.skworld:8443",
        ),
    ]
    monkeypatch.setenv("SKCHAT_NOSTR_RELAYS", "wss://relay.test")
    monkeypatch.setattr(
        "skcomms.transports.nostr._query_relay",
        lambda relay, filters: list(descriptors),
    )
    r = app_client.get("/sfu/candidates")
    assert r.status_code == 200
    hosts = r.json()["hosts"]
    fqids = {h["fqid"] for h in hosts}
    assert fqids == {"lumina@chef.skworld", "opus@chef.skworld"}
    lumina = next(h for h in hosts if h["fqid"] == "lumina@chef.skworld")
    assert lumina["auth_url"] == "https://lumina.skworld/sfu/get"
    assert lumina["sfu_ws_url"] == "wss://lumina.skworld:8443"


def test_candidates_dedups_and_skips_malformed(app_client, monkeypatch):
    good = build_focus_descriptor(
        host_fqid="lumina@chef.skworld",
        auth_url="https://lumina.skworld/sfu/get",
        sfu_ws_url="wss://lumina.skworld:8443",
    )
    # a duplicate of the same host + a malformed (non-JSON content) event
    dup = build_focus_descriptor(
        host_fqid="lumina@chef.skworld",
        auth_url="https://lumina.skworld/sfu/get",
        sfu_ws_url="wss://lumina.skworld:8443",
    )
    malformed = {"kind": 30078, "content": "not json {{"}
    monkeypatch.setenv("SKCHAT_NOSTR_RELAYS", "wss://relay.test")
    monkeypatch.setattr(
        "skcomms.transports.nostr._query_relay",
        lambda relay, filters: [good, dup, malformed],
    )
    r = app_client.get("/sfu/candidates")
    assert r.status_code == 200
    hosts = r.json()["hosts"]
    assert len(hosts) == 1
    assert hosts[0]["fqid"] == "lumina@chef.skworld"


def test_candidates_never_500_on_relay_failure(app_client, monkeypatch):
    def _boom(relay, filters):
        raise RuntimeError("relay exploded")

    monkeypatch.setenv("SKCHAT_NOSTR_RELAYS", "wss://relay.test")
    monkeypatch.setattr("skcomms.transports.nostr._query_relay", _boom)
    r = app_client.get("/sfu/candidates")
    assert r.status_code == 200
    assert r.json() == {"hosts": []}
