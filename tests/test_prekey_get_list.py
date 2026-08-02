"""PQC multi-device fanout (Phase 1), Task 6 - GET /prekey returns the slot list.

``GET /api/v1/prekey/<peer>`` now returns BOTH:

* ``prekeys`` - every published device slot, newest first (the multi-slot list
  from :func:`skchat.pq_prekeys.load_peer_bundles`), so the sender can fan out.
* ``prekey`` - the newest single slot, kept for pqdm1-only back-compat callers.

A peer that published two devices gets both slots in ``prekeys`` (with ``prekey``
== the newest one); a peer that published nothing gets an empty ``prekeys`` list
and a classical placeholder ``prekey`` (unchanged behaviour for that field).
"""

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def env(tmp_path, monkeypatch):
    """daemon_proxy + pq_prekeys bound to an isolated SKCHAT_HOME."""
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    from skchat import daemon_proxy, pq_prekeys

    importlib.reload(pq_prekeys)
    importlib.reload(daemon_proxy)
    app = FastAPI()
    app.include_router(daemon_proxy.router)
    return daemon_proxy, pq_prekeys, TestClient(app)


def _bundle(key_id: str, ts: int) -> dict:
    return {
        "suite": "x25519-mlkem768",
        "hybrid_public_hex": "00" * 16 + key_id,
        "key_id": key_id,
        "device_id": f"dev-{key_id}",
        "last_published": ts,
    }


def test_returns_both_fields_for_two_slots(env):
    _daemon, pq, client = env
    pq.store_peer_bundle("chef", _bundle("aaaaaaaaaaaaaaaa", ts=1))
    pq.store_peer_bundle("chef", _bundle("bbbbbbbbbbbbbbbb", ts=2))

    resp = client.get("/api/v1/prekey/chef")
    assert resp.status_code == 200
    body = resp.json()

    # prekeys carries every device slot, newest first.
    assert isinstance(body["prekeys"], list)
    kids = [b["key_id"] for b in body["prekeys"]]
    assert set(kids) == {"aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"}
    assert kids[0] == "bbbbbbbbbbbbbbbb"  # ts=2 sorts first

    # prekey back-compat field == the newest slot.
    assert body["prekey"]["key_id"] == "bbbbbbbbbbbbbbbb"


def test_unpublished_peer_gets_empty_list_and_classical_prekey(env):
    _daemon, pq, client = env
    resp = client.get("/api/v1/prekey/nobody")
    assert resp.status_code == 200
    body = resp.json()
    assert body["prekeys"] == []
    # The classical placeholder for the single-slot field stays as-is.
    assert body["prekey"]["suite"] == pq.CLASSICAL_SUITE
    assert body["prekey"]["hybrid_public_hex"] == ""


def test_lumina_returns_own_bundle_in_both_fields(env):
    _daemon, pq, client = env
    resp = client.get("/api/v1/prekey/lumina")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["prekeys"], list) and len(body["prekeys"]) == 1
    assert body["prekeys"][0] == body["prekey"]
