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


def test_connectivity_ice_rejects_unpaired(client):
    r = client.get("/connectivity/ice", params={"peer": "nobody@x.y"})
    assert r.status_code == 404


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
