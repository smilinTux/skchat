"""The prekey intake rejects an empty owner with a 400, not a 500.

Card c5bbb20d, HTTP layer. ``pq_prekeys._peer_dir`` is the hard chokepoint and
raises ValueError, which alone would surface as an uncaught 500. The route
rejects the same input up front so a caller gets a clean 400 and no stack trace.

Both layers are deliberate: the route guard is the friendly error, and the
_peer_dir guard is the one that still holds for any OTHER caller that reaches
the store without going through this route.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def PQ(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    from skchat import pq_prekeys

    importlib.reload(pq_prekeys)
    return pq_prekeys


@pytest.fixture()
def client(PQ):
    from skchat import daemon_proxy

    app = FastAPI()
    app.include_router(daemon_proxy.router)
    return TestClient(app, raise_server_exceptions=False)


def _bundle(**over) -> dict:
    b = {
        "suite": "x25519-mlkem768",
        "hybrid_public_hex": "ff" * 32,
        "key_id": "deadbeefdeadbeef",
    }
    b.update(over)
    return b


@pytest.mark.parametrize("owner", ["@x", "@", "capauth:@evil.io", "   "])
def test_empty_owner_is_a_400_not_a_500(client, owner):
    resp = client.post("/api/v1/prekey", json=_bundle(owner=owner))
    assert resp.status_code == 400, (
        f"owner={owner!r} should be a clean 400, got {resp.status_code}"
    )


def test_the_hijack_attempt_is_rejected_end_to_end(client, PQ):
    """The actual attack shape: empty owner + the victim's name as key_id."""
    PQ.store_peer_bundle(
        "lumina",
        {
            "suite": "x25519-mlkem768",
            "hybrid_public_hex": "aa" * 32,
            "key_id": "7a8ab00748c2bf47",
            "last_published": 1000,
        },
    )

    resp = client.post(
        "/api/v1/prekey",
        json=_bundle(owner="@x", key_id="lumina", last_published=99999),
    )

    assert resp.status_code == 400
    assert PQ.load_peer_bundle("lumina")["hybrid_public_hex"] == "aa" * 32
    root = PQ._pqc_dir() / "peers"
    assert sorted(p.name for p in root.iterdir()) == ["lumina"]


def test_a_normal_publish_still_works(client, PQ):
    """The default path (no owner field at all) is unchanged."""
    resp = client.post("/api/v1/prekey", json=_bundle())
    assert resp.status_code == 200
    assert PQ.load_peer_bundle("chef") is not None


def test_an_explicit_normal_owner_still_works(client, PQ):
    resp = client.post("/api/v1/prekey", json=_bundle(owner="capauth:lumina@skworld.io"))
    assert resp.status_code == 200
    assert PQ.load_peer_bundle("lumina") is not None
