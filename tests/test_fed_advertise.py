"""C2 runtime focus-advertise tests.

Covers the producer side that was missing: conf-create (and the advertise helper)
publish a focus descriptor + Space-state + membership to the relay, and a peer's
discovery client elects the advertised host from those published events.

All relay I/O is faked — no live Nostr relays, no SFU, no network.
"""

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.conf.room import ConfRegistry
from skchat.conf.routes import register_conf_routes
from skchat.spaces.federation.advertise import advertise_conf, advertise_focus
from skchat.spaces.federation.discovery import FederationDiscoveryClient
from skchat.spaces.federation.events import FOCUS_KIND, MEMBERSHIP_KIND, SPACE_KIND
from skchat.spaces.federation.nostr_io import FederationNostr

_KEY, _SECRET = "test-key", "test-secret-0123456789"


class _Recorder:
    """Fake Nostr publish/query seam — records published events, replays on query."""

    def __init__(self):
        self.published = []

    def publish(self, event):
        self.published.append(event)
        return True

    def query(self, filters):
        kinds = filters.get("kinds") or []
        return [e for e in self.published if e.get("kind") in kinds]

    def nostr(self):
        return FederationNostr(publish=self.publish, query=self.query)


# ── advertise helper ─────────────────────────────────────────────────────────


def test_advertise_focus_publishes_descriptor_space_and_membership(monkeypatch):
    monkeypatch.setenv("SKCHAT_NOSTR_RELAYS", "ws://relay:7447")
    monkeypatch.setenv("SKCHAT_LIVEKIT_PUBLIC_URL", "wss://noroc2027.tail204f0c.ts.net/livekit-ws")
    rec = _Recorder()
    ok = advertise_focus(
        host_fqid="lumina@chef.skworld",
        room="conf-abc",
        title="Standup",
        mint_path="/conf/conf-abc/federated-token",
        nostr=rec.nostr(),
    )
    assert ok is True
    kinds = [e["kind"] for e in rec.published]
    assert FOCUS_KIND in kinds and SPACE_KIND in kinds and MEMBERSHIP_KIND in kinds

    # focus descriptor carries the PUBLIC sfu + derived auth_url (no hardcoding)
    import json

    focus = next(e for e in rec.published if e["kind"] == FOCUS_KIND)
    content = json.loads(focus["content"])
    assert content["sfu_ws_url"] == "wss://noroc2027.tail204f0c.ts.net/livekit-ws"
    assert content["auth_url"] == (
        "https://noroc2027.tail204f0c.ts.net/conf/conf-abc/federated-token"
    )
    assert content["host_fqid"] == "lumina@chef.skworld"

    # membership ties the room to this host as its preferred focus
    member = next(e for e in rec.published if e["kind"] == MEMBERSHIP_KIND)
    tags = {t[0]: t[1] for t in member["tags"]}
    assert tags["foci_preferred"] == "lumina@chef.skworld"
    assert tags["a"] == f"{SPACE_KIND}:conf-abc"


def test_advertise_focus_no_relays_is_noop(monkeypatch):
    monkeypatch.delenv("SKCHAT_NOSTR_RELAYS", raising=False)
    monkeypatch.delenv("SKCHAT_LIVEKIT_PUBLIC_URL", raising=False)
    # no relays AND no injected nostr -> best-effort no-op, returns False, no raise
    assert advertise_focus(
        host_fqid="h@x", room="conf-y", title="T", mint_path="/conf/conf-y/federated-token"
    ) is False


def test_advertise_focus_never_raises_on_publish_error(monkeypatch):
    monkeypatch.setenv("SKCHAT_NOSTR_RELAYS", "ws://relay:7447")
    monkeypatch.setenv("SKCHAT_LIVEKIT_PUBLIC_URL", "wss://h/livekit-ws")

    def _boom(_ev):
        raise RuntimeError("relay down")

    nostr = FederationNostr(publish=_boom, query=lambda f: [])
    # a relay failure must be swallowed (advertise is best-effort)
    assert advertise_conf(
        host_fqid="h@x", room="conf-z", title="T", nostr=nostr
    ) is False


def test_advertise_uses_explicit_public_webui_base(monkeypatch):
    monkeypatch.setenv("SKCHAT_NOSTR_RELAYS", "ws://relay:7447")
    monkeypatch.setenv("SKCHAT_LIVEKIT_PUBLIC_URL", "wss://sfu.example/livekit-ws")
    monkeypatch.setenv("SKCHAT_PUBLIC_WEBUI_URL", "https://webui.example/")
    rec = _Recorder()
    advertise_conf(host_fqid="h@x", room="conf-q", title="T", nostr=rec.nostr())
    import json

    focus = next(e for e in rec.published if e["kind"] == FOCUS_KIND)
    # auth_url uses the explicit webui base, not the SFU host
    assert json.loads(focus["content"])["auth_url"] == (
        "https://webui.example/conf/conf-q/federated-token"
    )


# ── conf create triggers advertise ───────────────────────────────────────────


@pytest.fixture
def conf_client(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", _KEY)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", _SECRET)
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", "ws://test-sfu:7880")

    calls = []

    def _fake_advertiser(*, host_fqid, room, title):
        calls.append({"host_fqid": host_fqid, "room": room, "title": title})

    app = FastAPI()
    register_conf_routes(
        app,
        registry=ConfRegistry(path=tmp_path / "confs.json"),
        advertiser=_fake_advertiser,
    )
    client = TestClient(app)
    client.advertise_calls = calls  # type: ignore[attr-defined]
    return client


def test_conf_create_triggers_focus_advertise(conf_client):
    r = conf_client.post(
        "/conf/create", json={"host_fqid": "lumina@chef.skworld", "title": "Standup", "slug": "su"}
    )
    assert r.status_code == 200
    room = r.json()["room"]
    # the injected advertiser was called exactly once with the created room
    assert len(conf_client.advertise_calls) == 1
    call = conf_client.advertise_calls[0]
    assert call["host_fqid"] == "lumina@chef.skworld"
    assert call["room"] == room
    assert call["title"] == "Standup"


def test_conf_create_succeeds_when_advertiser_raises(tmp_path, monkeypatch):
    """A relay/advertise failure must NEVER fail the conf create."""
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", _KEY)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", _SECRET)
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", "ws://test-sfu:7880")

    def _boom(**_kw):
        raise RuntimeError("relay unreachable")

    app = FastAPI()
    register_conf_routes(
        app, registry=ConfRegistry(path=tmp_path / "confs.json"), advertiser=_boom
    )
    client = TestClient(app)
    r = client.post("/conf/create", json={"host_fqid": "h@x", "title": "T", "slug": "s"})
    assert r.status_code == 200  # create still succeeds despite advertise blowing up
    jwt.decode(
        r.json()["token"], _SECRET, algorithms=["HS256"], options={"verify_aud": False}
    )


# ── peer discovery parses the published focus + elects the host ──────────────


def test_published_focus_is_discoverable_and_elected(monkeypatch):
    """End-to-end producer→consumer: advertise_focus publishes to a shared fake
    relay, and a FederationDiscoveryClient over that SAME relay elects the host."""
    monkeypatch.setenv("SKCHAT_NOSTR_RELAYS", "ws://relay:7447")
    monkeypatch.setenv("SKCHAT_LIVEKIT_PUBLIC_URL", "wss://h.example/livekit-ws")
    rec = _Recorder()

    # producer side: instance publishes its focus for room conf-fed
    advertise_conf(
        host_fqid="lumina@chef.skworld", room="conf-fed", title="Town Hall", nostr=rec.nostr()
    )

    # consumer side: a peer discovers + elects over the SAME relay store
    client = FederationDiscoveryClient(nostr=rec.nostr())
    elected = client.discover_and_elect("conf-fed")
    assert elected.fqid == "lumina@chef.skworld"
    assert elected.sfu_ws_url == "wss://h.example/livekit-ws"
    assert elected.auth_url == "https://h.example/conf/conf-fed/federated-token"


def test_conf_candidates_lists_advertised_confs(tmp_path, monkeypatch):
    """GET /conf/candidates surfaces a focus+space-state advertised on the relay."""
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", _KEY)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", _SECRET)
    monkeypatch.setenv("SKCHAT_NOSTR_RELAYS", "ws://relay:7447")
    monkeypatch.setenv("SKCHAT_LIVEKIT_PUBLIC_URL", "wss://h.example/livekit-ws")

    rec = _Recorder()
    advertise_conf(
        host_fqid="lumina@chef.skworld", room="conf-fed", title="Town Hall", nostr=rec.nostr()
    )

    # patch FederationNostr so the route's internal construction uses our recorder
    import skchat.spaces.federation.nostr_io as nio

    monkeypatch.setattr(
        nio, "FederationNostr", lambda relays=None: rec.nostr(), raising=True
    )

    app = FastAPI()
    register_conf_routes(app, registry=ConfRegistry(path=tmp_path / "confs.json"))
    client = TestClient(app)
    confs = client.get("/conf/candidates").json()["confs"]
    found = next(c for c in confs if c["room"] == "conf-fed")
    assert found["host_fqid"] == "lumina@chef.skworld"
    assert found["sfu_ws_url"] == "wss://h.example/livekit-ws"
    assert found["auth_url"] == "https://h.example/conf/conf-fed/federated-token"
    assert found["is_conf"] is True
