"""Tests for call_routes — /call/start, /call/answer, /call/incoming, /connectivity/ice."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import skchat.call_routes as cr
from skchat.call_session import build_invite_body, derive_room


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(cr, "_list_peers", lambda: {"lumina@chef.skworld": {"fingerprint": "FP"}})
    monkeypatch.setattr(cr, "_self_fqid", lambda: "opus@chef.skworld")
    monkeypatch.setattr(cr, "_have_creds", lambda: True)
    monkeypatch.setattr(
        cr, "_mint_token", lambda identity, name, room, ttl: f"tok::{identity}::{room}"
    )
    sent = []
    monkeypatch.setattr(cr, "_send_invite", lambda **kw: sent.append(kw))
    monkeypatch.setattr(cr, "_alert_operator", lambda **kw: None)
    app = FastAPI()
    cr.register_call_routes(app)
    c = TestClient(app)
    c._sent = sent
    return c


def test_call_start_rejects_unpaired(client):
    r = client.post("/call/start", json={"peer": "stranger@x.y"})
    assert r.status_code == 404


def test_call_start_mints_and_rings(client):
    r = client.post("/call/start", json={"peer": "lumina@chef.skworld"})
    assert r.status_code == 200
    data = r.json()
    expected_room = derive_room("opus@chef.skworld", "lumina@chef.skworld")
    assert data["room"] == expected_room
    assert data["token"] == f"tok::opus@chef.skworld::{expected_room}"
    assert data["peer_fqid"] == "lumina@chef.skworld"
    assert len(client._sent) == 1


def test_call_answer_mints_same_room_without_ringing(client):
    r = client.post("/call/answer", json={"peer": "lumina@chef.skworld"})
    assert r.status_code == 200
    data = r.json()
    assert data["room"] == derive_room("opus@chef.skworld", "lumina@chef.skworld")
    assert len(client._sent) == 0


# ── Task 5: /call/incoming ────────────────────────────────────────────────────


def _env(subject, from_fqid, to_fqid, room):
    body = build_invite_body(
        from_fqid=from_fqid,
        to_fqid=to_fqid,
        room=room,
        livekit_url="wss://x:8443",
    )
    return SimpleNamespace(subject=subject, from_fqid=from_fqid, to_fqid=to_fqid, body=body)


def test_incoming_returns_only_invites_for_self(client, monkeypatch):
    inbox = [
        (
            _env("CALL_INVITE", "lumina@chef.skworld", "opus@chef.skworld", "call-r1"),
            SimpleNamespace(valid=True),
        ),
        (
            _env("text/plain note", "lumina@chef.skworld", "opus@chef.skworld", "call-x"),
            SimpleNamespace(valid=True),
        ),
        (
            _env("CALL_INVITE", "stranger@x.y", "someone@else.z", "call-r2"),
            SimpleNamespace(valid=True),
        ),
    ]
    monkeypatch.setattr(cr, "_read_inbox", lambda: inbox)
    r = client.get("/call/incoming")
    assert r.status_code == 200
    invites = r.json()["invites"]
    assert len(invites) == 1
    assert invites[0]["from_fqid"] == "lumina@chef.skworld"
    assert invites[0]["room"] == "call-r1"


# ── Task 6: /connectivity/ice ─────────────────────────────────────────────────


def test_connectivity_ice_for_paired_peer(client):
    r = client.get("/connectivity/ice", params={"peer": "lumina@chef.skworld"})
    assert r.status_code == 200
    data = r.json()
    assert "ice_servers" in data and "preferred_tier" in data


def test_connectivity_ice_unpaired_peer_falls_back_not_404(client, monkeypatch):
    # A guest/conf participant's peer id is never paired. It must NOT 404 (which
    # would deny the guest any TURN config). The default TestClient host is
    # off-tailnet, so an unpaired peer gets the relay tier (STUN/TURN), same as
    # a paired one from the same client.
    monkeypatch.delenv("SKCHAT_TURN_SECRET", raising=False)
    monkeypatch.setenv("SKCHAT_PUBLIC_TURN_ENABLED", "true")
    r = client.get("/connectivity/ice", params={"peer": "nobody@x.y"})
    assert r.status_code == 200
    data = r.json()
    assert data["on_tailnet"] is False
    assert data["preferred_tier"] == 3
    assert data["ice_servers"]


def test_connectivity_ice_guest_offtailnet_gets_sovereign_turn(monkeypatch):
    # A guest (peer "guest-903ekz", never paired) off the tailnet must receive
    # the SOVEREIGN turns: relay with a fresh ephemeral HMAC cred, not a 404.
    monkeypatch.setattr(cr, "_list_peers", lambda: {"lumina@chef.skworld": {"fingerprint": "FP"}})
    monkeypatch.setattr(cr, "_self_fqid", lambda: "opus@chef.skworld")
    monkeypatch.setenv("SKCHAT_TURN_SECRET", "s3cr3t")
    monkeypatch.setenv("SKCHAT_TURN_URLS", "turns:turn.skworld.io:5349,turn:turn.skworld.io:3478")
    app = FastAPI()
    cr.register_call_routes(app)
    # testclient host is off-tailnet.
    guest_client = TestClient(app)
    r = guest_client.get("/connectivity/ice", params={"peer": "guest-903ekz"})
    assert r.status_code == 200
    data = r.json()
    assert data["on_tailnet"] is False
    assert data["preferred_tier"] == 3
    turn_urls = [u for srv in data["ice_servers"] for u in srv["urls"] if u.startswith("turn")]
    assert any(u.startswith("turns:turn.skworld.io") for u in turn_urls), turn_urls
    # ephemeral cred must be present on the sovereign relay entry
    relay = [srv for srv in data["ice_servers"] if srv.get("credential")]
    assert relay and relay[0]["username"] and relay[0]["credential"]


def test_connectivity_ice_guest_ontailnet_gets_relay_free_path(monkeypatch):
    # An on-tailnet request with an unpaired guest peer gets the direct Tier-1
    # (relay-free) config, same as a paired peer would from that client.
    monkeypatch.setattr(cr, "_list_peers", lambda: {"lumina@chef.skworld": {"fingerprint": "FP"}})
    monkeypatch.setattr(cr, "_self_fqid", lambda: "opus@chef.skworld")
    app = FastAPI()
    cr.register_call_routes(app)
    tailnet_client = TestClient(app, client=("100.64.0.5", 12345))
    r = tailnet_client.get("/connectivity/ice", params={"peer": "guest-903ekz"})
    assert r.status_code == 200
    data = r.json()
    assert data["on_tailnet"] is True
    assert data["preferred_tier"] == 1
    assert data["ice_servers"] == []


def test_connectivity_ice_no_peer_arg_still_returns_config(monkeypatch):
    # The peer arg is now optional: a conf client may omit it entirely and still
    # get an ICE config classified from its own connection.
    monkeypatch.setattr(cr, "_list_peers", lambda: {"lumina@chef.skworld": {"fingerprint": "FP"}})
    monkeypatch.setattr(cr, "_self_fqid", lambda: "opus@chef.skworld")
    monkeypatch.delenv("SKCHAT_TURN_SECRET", raising=False)
    monkeypatch.setenv("SKCHAT_PUBLIC_TURN_ENABLED", "true")
    app = FastAPI()
    cr.register_call_routes(app)
    r = TestClient(app).get("/connectivity/ice")
    assert r.status_code == 200
    data = r.json()
    assert data["on_tailnet"] is False
    assert data["ice_servers"]


def test_connectivity_ice_offtailnet_caller_gets_relay_servers(client, monkeypatch):
    # Bug #2 regression: on_tailnet must be derived from the requesting
    # connection, not hardcoded True. The default TestClient host
    # ("testclient") is not a private/tailnet address, so a caller off the
    # tailnet (mobile data, home wifi w/o Tailscale, a public/Funnel guest)
    # must fall through to the STUN/TURN relay tier instead of an empty list.
    monkeypatch.delenv("SKCHAT_TURN_SECRET", raising=False)
    monkeypatch.setenv("SKCHAT_PUBLIC_TURN_ENABLED", "true")
    r = client.get("/connectivity/ice", params={"peer": "lumina@chef.skworld"})
    assert r.status_code == 200
    data = r.json()
    assert data["on_tailnet"] is False
    assert data["preferred_tier"] == 3
    assert data["ice_servers"], "expected non-empty iceServers (STUN/TURN) for a non-tailnet caller"


def test_connectivity_ice_tailnet_caller_gets_tailnet_path(monkeypatch):
    monkeypatch.setattr(cr, "_list_peers", lambda: {"lumina@chef.skworld": {"fingerprint": "FP"}})
    monkeypatch.setattr(cr, "_self_fqid", lambda: "opus@chef.skworld")
    app = FastAPI()
    cr.register_call_routes(app)
    # A caller connecting from a Tailscale CGNAT address (100.64.0.0/10).
    tailnet_client = TestClient(app, client=("100.64.0.5", 12345))
    r = tailnet_client.get("/connectivity/ice", params={"peer": "lumina@chef.skworld"})
    assert r.status_code == 200
    data = r.json()
    assert data["on_tailnet"] is True
    assert data["preferred_tier"] == 1
    assert data["ice_servers"] == []


def test_connectivity_ice_funnel_loopback_caller_gets_relay(monkeypatch):
    # Regression: behind Tailscale Funnel, a genuine off-tailnet phone's HTTPS
    # request is proxied by tailscaled and arrives at the app as loopback
    # (127.0.0.1). It must NOT be misclassified as on-tailnet. The real client
    # (a public IP) is carried in X-Forwarded-For + the Tailscale-Funnel-Request
    # signal, so the caller must fall through to the STUN/TURN relay tier.
    monkeypatch.setattr(cr, "_list_peers", lambda: {"lumina@chef.skworld": {"fingerprint": "FP"}})
    monkeypatch.setattr(cr, "_self_fqid", lambda: "opus@chef.skworld")
    monkeypatch.delenv("SKCHAT_TURN_SECRET", raising=False)
    monkeypatch.setenv("SKCHAT_PUBLIC_TURN_ENABLED", "true")
    app = FastAPI()
    cr.register_call_routes(app)
    # Direct peer is loopback (the local tailscaled proxy).
    funnel_client = TestClient(app, client=("127.0.0.1", 54321))
    r = funnel_client.get(
        "/connectivity/ice",
        params={"peer": "lumina@chef.skworld"},
        headers={
            "Tailscale-Funnel-Request": "?1",
            "X-Forwarded-For": "203.0.113.7",  # the real off-tailnet client
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["on_tailnet"] is False
    assert data["preferred_tier"] == 3
    assert data["ice_servers"], "expected STUN/TURN servers for a Funnel-proxied off-tailnet caller"


def test_connectivity_ice_forwarded_tailnet_client_stays_direct(monkeypatch):
    # A real tailnet client (100.64.0.0/10) reaching us via the loopback proxy
    # (X-Forwarded-For carries its 100.x address) must still be treated as
    # on-tailnet and get the relay-free Tier-1 path.
    monkeypatch.setattr(cr, "_list_peers", lambda: {"lumina@chef.skworld": {"fingerprint": "FP"}})
    monkeypatch.setattr(cr, "_self_fqid", lambda: "opus@chef.skworld")
    app = FastAPI()
    cr.register_call_routes(app)
    proxied = TestClient(app, client=("127.0.0.1", 54321))
    r = proxied.get(
        "/connectivity/ice",
        params={"peer": "lumina@chef.skworld"},
        headers={"X-Forwarded-For": "100.100.5.9"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["on_tailnet"] is True
    assert data["preferred_tier"] == 1
    assert data["ice_servers"] == []


def test_connectivity_ice_lan_caller_stays_direct(monkeypatch):
    # A LAN 192.168.x caller behaves as before: on-tailnet-equivalent direct
    # path (Tier 1, no relay).
    monkeypatch.setattr(cr, "_list_peers", lambda: {"lumina@chef.skworld": {"fingerprint": "FP"}})
    monkeypatch.setattr(cr, "_self_fqid", lambda: "opus@chef.skworld")
    app = FastAPI()
    cr.register_call_routes(app)
    lan_client = TestClient(app, client=("192.168.0.42", 40000))
    r = lan_client.get("/connectivity/ice", params={"peer": "lumina@chef.skworld"})
    assert r.status_code == 200
    data = r.json()
    assert data["on_tailnet"] is True
    assert data["preferred_tier"] == 1
    assert data["ice_servers"] == []


def test_connectivity_ice_bare_loopback_no_forward_gets_relay(monkeypatch):
    # Bare loopback with no forwarding info can no longer prove tailnet
    # membership (it could be a Funnel request that stripped headers), so it is
    # classified OFF-tailnet and reaches the relay tier.
    monkeypatch.setattr(cr, "_list_peers", lambda: {"lumina@chef.skworld": {"fingerprint": "FP"}})
    monkeypatch.setattr(cr, "_self_fqid", lambda: "opus@chef.skworld")
    monkeypatch.delenv("SKCHAT_TURN_SECRET", raising=False)
    monkeypatch.setenv("SKCHAT_PUBLIC_TURN_ENABLED", "true")
    app = FastAPI()
    cr.register_call_routes(app)
    loopback_client = TestClient(app, client=("127.0.0.1", 12345))
    r = loopback_client.get("/connectivity/ice", params={"peer": "lumina@chef.skworld"})
    assert r.status_code == 200
    data = r.json()
    assert data["on_tailnet"] is False
    assert data["preferred_tier"] == 3
    assert data["ice_servers"]


def test_call_start_503_no_creds(client, monkeypatch):
    monkeypatch.setattr(cr, "_have_creds", lambda: False)
    r = client.post("/call/start", json={"peer": "lumina@chef.skworld"})
    assert r.status_code == 503


def test_call_start_rejects_ambiguous_bare_name(client, monkeypatch):
    monkeypatch.setattr(
        cr,
        "_list_peers",
        lambda: {"lumina@chef.skworld": {}, "lumina@other.world": {}},
    )
    r = client.post("/call/start", json={"peer": "lumina"})
    assert r.status_code == 409
    assert "ambiguous" in r.json()["detail"]


def test_call_start_bare_name_resolves(client):
    r = client.post("/call/start", json={"peer": "lumina"})
    assert r.status_code == 200
    assert r.json()["peer_fqid"] == "lumina@chef.skworld"


def test_call_start_missing_peer_400(client):
    r = client.post("/call/start", json={})
    assert r.status_code == 400


def test_incoming_skips_malformed_invite(client, monkeypatch):
    good = _env("CALL_INVITE", "lumina@chef.skworld", "opus@chef.skworld", "call-good")
    bad = SimpleNamespace(
        subject="CALL_INVITE",
        from_fqid="lumina@chef.skworld",
        to_fqid="opus@chef.skworld",
        body="{not json",
    )
    monkeypatch.setattr(
        cr,
        "_read_inbox",
        lambda: [(bad, SimpleNamespace(valid=True)), (good, SimpleNamespace(valid=True))],
    )
    r = client.get("/call/incoming")
    invites = r.json()["invites"]
    assert len(invites) == 1
    assert invites[0]["room"] == "call-good"


def test_incoming_rejects_spoofed_from_fqid(client, monkeypatch):
    # Bug #4 regression: a validly-signed envelope from a paired-but-different
    # peer must not be able to make its JSON body claim a different from_fqid
    # (e.g. "chef@skworld.io") than the envelope's cryptographically verified
    # sender. The body's from_fqid is the sender's own unattested claim.
    spoofed_body = build_invite_body(
        from_fqid="chef@skworld.io",  # attacker-controlled claim inside the JSON
        to_fqid="opus@chef.skworld",
        room="call-spoofed",
        livekit_url="wss://x:8443",
    )
    envelope = SimpleNamespace(
        subject="CALL_INVITE",
        from_fqid="stranger@x.y",  # the envelope's actual, verified sender
        to_fqid="opus@chef.skworld",
        body=spoofed_body,
    )
    monkeypatch.setattr(cr, "_read_inbox", lambda: [(envelope, SimpleNamespace(valid=True))])
    r = client.get("/call/incoming")
    assert r.status_code == 200
    assert r.json()["invites"] == []


def test_incoming_accepts_matching_from_fqid(client, monkeypatch):
    envelope = SimpleNamespace(
        subject="CALL_INVITE",
        from_fqid="lumina@chef.skworld",
        to_fqid="opus@chef.skworld",
        body=build_invite_body(
            from_fqid="lumina@chef.skworld",
            to_fqid="opus@chef.skworld",
            room="call-legit",
            livekit_url="wss://x:8443",
        ),
    )
    monkeypatch.setattr(cr, "_read_inbox", lambda: [(envelope, SimpleNamespace(valid=True))])
    r = client.get("/call/incoming")
    invites = r.json()["invites"]
    assert len(invites) == 1
    assert invites[0]["room"] == "call-legit"


def test_incoming_empty_inbox(client, monkeypatch):
    monkeypatch.setattr(cr, "_read_inbox", lambda: [])
    r = client.get("/call/incoming")
    assert r.status_code == 200
    assert r.json()["invites"] == []


def test_incoming_skips_unverified_invite(client, monkeypatch):
    from types import SimpleNamespace

    env = _env("CALL_INVITE", "lumina@chef.skworld", "opus@chef.skworld", "call-unsigned")
    monkeypatch.setattr(cr, "_read_inbox", lambda: [(env, SimpleNamespace(valid=False))])
    r = client.get("/call/incoming")
    assert r.json()["invites"] == []


def test_webui_registers_call_routes():
    from skchat.webui import app

    paths = {r.path for r in app.routes}
    assert "/call/start" in paths
    assert "/call/answer" in paths
    assert "/call/incoming" in paths
    assert "/connectivity/ice" in paths


def test_call_peers_lists_paired(client):
    r = client.get("/call/peers")
    assert r.status_code == 200
    peers = r.json()["peers"]
    assert any(p["fqid"] == "lumina@chef.skworld" for p in peers)
    lumina = next(p for p in peers if p["fqid"] == "lumina@chef.skworld")
    assert lumina["fingerprint"] == "FP"


def test_call_start_threads_topic_and_alerts(client, monkeypatch):
    alerts = []
    monkeypatch.setattr(cr, "_alert_operator", lambda **kw: alerts.append(kw))
    r = client.post(
        "/call/start", json={"peer": "lumina@chef.skworld", "topic": "ingest debugging"}
    )
    assert r.status_code == 200
    assert client._sent[0]["topic"] == "ingest debugging"  # invite carried the topic
    assert len(alerts) == 1 and alerts[0]["topic"] == "ingest debugging"
