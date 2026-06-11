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
    monkeypatch.setattr(
        cr, "_list_peers", lambda: {"lumina@chef.skworld": {"fingerprint": "FP"}}
    )
    monkeypatch.setattr(cr, "_self_fqid", lambda: "opus@chef.skworld")
    monkeypatch.setattr(cr, "_have_creds", lambda: True)
    monkeypatch.setattr(cr, "_mint_token", lambda identity, name, room, ttl: f"tok::{identity}::{room}")
    sent = []
    monkeypatch.setattr(cr, "_send_invite", lambda **kw: sent.append(kw))
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
        from_fqid=from_fqid, to_fqid=to_fqid, room=room,
        livekit_url="wss://x:8443",
    )
    return SimpleNamespace(subject=subject, from_fqid=from_fqid, to_fqid=to_fqid, body=body)


def test_incoming_returns_only_invites_for_self(client, monkeypatch):
    inbox = [
        (_env("CALL_INVITE", "lumina@chef.skworld", "opus@chef.skworld", "call-r1"), None),
        (_env("text/plain note", "lumina@chef.skworld", "opus@chef.skworld", "call-x"), None),
        (_env("CALL_INVITE", "stranger@x.y", "someone@else.z", "call-r2"), None),
    ]
    monkeypatch.setattr(cr, "_read_inbox", lambda: inbox)
    r = client.get("/call/incoming")
    assert r.status_code == 200
    invites = r.json()["invites"]
    assert len(invites) == 1
    assert invites[0]["from_fqid"] == "lumina@chef.skworld"
    assert invites[0]["room"] == "call-r1"
