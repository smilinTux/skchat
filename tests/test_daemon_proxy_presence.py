"""Tests for the real ``POST /api/v1/presence`` endpoint (SEAM 5 / P3).

The endpoint used to be a no-op stub (``{"ok": true}``). It is now a real read
backed by the existing ``PresenceCache`` (``skchat.presence``): given a peer it
returns that peer's actual online/last-seen state; an unknown peer degrades to
``offline`` (absent), never an error.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat import daemon_proxy
from skchat.presence import PresenceCache, PresenceState


@pytest.fixture
def cache(tmp_path):
    return PresenceCache(cache_file=tmp_path / "presence_cache.json")


@pytest.fixture
def client(tmp_path, monkeypatch, cache):
    # Isolate presence to a tmp-backed cache so we don't touch ~/.skchat.
    monkeypatch.setattr(daemon_proxy, "_PRESENCE", cache)

    app = FastAPI()
    app.include_router(daemon_proxy.router)
    return TestClient(app)


def test_known_online_peer_returns_real_state(client, cache):
    cache.record("capauth:lumina@skworld.io", PresenceState.ONLINE)

    r = client.post("/api/v1/presence", json={"peer": "capauth:lumina@skworld.io"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["peer"] == "capauth:lumina@skworld.io"
    assert body["state"] == "online"
    assert body["online"] is True
    assert body["last_seen"] is not None


def test_unknown_peer_returns_offline_not_error(client):
    r = client.post("/api/v1/presence", json={"peer": "capauth:nobody@skworld.io"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["peer"] == "capauth:nobody@skworld.io"
    assert body["state"] == "offline"
    assert body["online"] is False
    assert body["last_seen"] is None


def test_stale_peer_is_offline(client, cache):
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    cache.record("capauth:ghost@skworld.io", PresenceState.ONLINE, timestamp=old)

    r = client.post("/api/v1/presence", json={"peer": "capauth:ghost@skworld.io"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "offline"
    assert body["online"] is False
    # last_seen is still surfaced even when the derived status is offline.
    assert body["last_seen"] is not None


def test_no_peer_lists_all_cached_entries(client, cache):
    cache.record("capauth:lumina@skworld.io", PresenceState.ONLINE)
    cache.record("capauth:chef@skworld.io", PresenceState.AWAY)

    r = client.post("/api/v1/presence", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    uris = {p["uri"] for p in body["peers"]}
    assert "capauth:lumina@skworld.io" in uris
    assert "capauth:chef@skworld.io" in uris


def test_empty_body_does_not_error(client):
    # No JSON body at all — must not 500.
    r = client.post("/api/v1/presence")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
