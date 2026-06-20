"""Tests for the federation observability surface (C4) — GET /federation/status.

Exercise the read-only status endpoint with every external source mocked
(relay query, trust policy, pinned peers, registry) and assert:

* the shape: identity / relays / trust / pinned_peers / discovered_focus /
  counts / errors are all present,
* it joins the mocked relay focus descriptors into ``discovered_focus``,
* it NEVER 500s — a sub-source raising degrades to empty + an ``errors`` note,
* the federation token counters surface and increment via ``incr``.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import federation_status as fs


@pytest.fixture(autouse=True)
def _reset_counters():
    # Counters are process-global; isolate each test.
    fs._COUNTERS.clear()
    fs._COUNTERS.update({"fed_tokens_minted": 0, "fed_tokens_redeemed": 0})
    yield


def _client() -> TestClient:
    app = FastAPI()
    fs.register_federation_status_routes(app)
    return TestClient(app)


# ── build_federation_status: shape + injected seams ──────────────────────────


def test_status_shape_with_injected_seams():
    out = fs.build_federation_status(
        fqid_fn=lambda: "lumina@chef.skworld",
        relays_fn=lambda: ["wss://relay.example"],
        trust_fn=lambda errs: {
            "configured": True,
            "full_access": ["jarvis@chef.skworld"],
            "default": "subscribe",
            "remote_max_role": "speaker",
        },
        peers_fn=lambda errs: ["jarvis@chef.skworld"],
        discover_fn=lambda relays, errs: [
            {
                "fqid": "jarvis@chef.skworld",
                "auth_url": "http://box-a:8765/conf/x/federated-token",
                "sfu_ws_url": "wss://box-a/livekit-ws",
            }
        ],
        counts_fn=lambda errs: {"live_confs": 2, "live_spaces": 1},
    )
    assert out["service"] == "skchat-federation"
    assert out["status"] == "ok"
    assert out["identity"]["fqid"] == "lumina@chef.skworld"
    assert out["relays"] == ["wss://relay.example"]
    assert out["trust"]["full_access"] == ["jarvis@chef.skworld"]
    assert out["pinned_peers"] == ["jarvis@chef.skworld"]
    assert out["discovered_focus"][0]["fqid"] == "jarvis@chef.skworld"
    assert out["counts"]["live_confs"] == 2
    assert out["counts"]["live_spaces"] == 1
    # token counters are merged into counts
    assert out["counts"]["fed_tokens_minted"] == 0
    assert out["counts"]["fed_tokens_redeemed"] == 0
    assert out["errors"] == []


def test_default_seams_record_errors_instead_of_raising(monkeypatch):
    # The CONTRACT is that each default seam swallows its own failure into the
    # ``errors`` list rather than propagating — so build_federation_status with
    # the default seams never raises even when a source is broken.
    def _boom_discover(relays, errs):
        errs.append("discovery: kaboom")
        return []

    out = fs.build_federation_status(
        fqid_fn=lambda: None,
        relays_fn=lambda: ["wss://relay"],
        trust_fn=lambda errs: {"configured": False, "full_access": [], "default": "deny"},
        peers_fn=lambda errs: [],
        discover_fn=_boom_discover,
        counts_fn=lambda errs: {"live_confs": 0, "live_spaces": 0},
    )
    assert out["status"] == "ok"
    assert out["identity"]["fqid"] is None
    assert "discovery: kaboom" in out["errors"]


def test_default_trust_view_degrades_when_unconfigured(tmp_path, monkeypatch):
    # Point TrustPolicy at a non-existent file → configured False, no raise.
    import skchat.spaces.federation.trust as trust_mod

    monkeypatch.setattr(trust_mod, "_DEFAULT_PATH", tmp_path / "nope.json")
    errors: list = []
    view = fs._trust_view(errors)
    assert view["configured"] is False
    assert view["default"] == "deny"
    assert errors == []


def test_pinned_peers_lists_asc_stems(tmp_path, monkeypatch):
    base = tmp_path / ".skchat" / "federation-peers"
    base.mkdir(parents=True)
    (base / "jarvis@chef.skworld.asc").write_text("KEY", encoding="utf-8")
    (base / "ava@chef.skworld.asc").write_text("KEY", encoding="utf-8")
    (base / "notakey.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(fs.Path, "home", staticmethod(lambda: tmp_path))
    errors: list = []
    peers = fs._pinned_peers(errors)
    assert peers == ["ava@chef.skworld", "jarvis@chef.skworld"]
    assert errors == []


# ── HTTP route: never 500 ────────────────────────────────────────────────────


def test_route_returns_200_and_shape(monkeypatch):
    monkeypatch.setattr(fs, "_self_fqid", lambda: "lumina@chef.skworld")
    monkeypatch.setattr(fs, "_relays", lambda: [])
    client = _client()
    r = client.get("/federation/status")
    assert r.status_code == 200
    body = r.json()
    for key in ("identity", "relays", "trust", "pinned_peers",
                "discovered_focus", "counts", "errors"):
        assert key in body


def test_route_never_500_on_assembly_failure(monkeypatch):
    monkeypatch.setattr(
        fs, "build_federation_status",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("kaboom")),
    )
    client = _client()
    r = client.get("/federation/status")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded"


# ── counters ─────────────────────────────────────────────────────────────────


def test_counters_increment_and_snapshot():
    assert fs.snapshot_counters()["fed_tokens_minted"] == 0
    fs.incr("fed_tokens_minted")
    fs.incr("fed_tokens_minted", 2)
    fs.incr("fed_tokens_redeemed")
    snap = fs.snapshot_counters()
    assert snap["fed_tokens_minted"] == 3
    assert snap["fed_tokens_redeemed"] == 1


def test_discovered_focus_joins_relay_descriptors(monkeypatch):
    # Fake a FederationNostr whose _query returns one valid focus descriptor.
    import skchat.spaces.federation.nostr_io as nio

    class _FakeNostr:
        def __init__(self, *a, **k):
            pass

        def _query(self, _filters):
            return [
                {
                    "content": (
                        '{"host_fqid":"jarvis@chef.skworld",'
                        '"auth_url":"http://box-a:8765/conf/x/federated-token",'
                        '"sfu_ws_url":"wss://box-a/livekit-ws"}'
                    )
                },
                {"content": "{not json"},  # hostile/malformed → skipped
            ]

    monkeypatch.setattr(nio, "FederationNostr", _FakeNostr)
    errors: list = []
    hosts = fs._discovered_focus(["wss://relay"], errors)
    assert len(hosts) == 1
    assert hosts[0]["fqid"] == "jarvis@chef.skworld"
    assert errors == []
