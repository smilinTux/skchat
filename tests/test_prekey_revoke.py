"""PQC multi-device fanout (Phase 1), Task 8 - revoke a device slot.

The operator can retire a single published device slot by ``key_id`` via
``DELETE /api/v1/prekey/<peer>/<key_id>``. The endpoint is operator-gated with
the shared ``skchat.guest._require_operator`` helper: a non-operator caller is
rejected (401/403) and no slot is touched; an operator call removes exactly that
slot from the multi-slot store (Task 3 ``remove_peer_bundle``).
"""

from __future__ import annotations

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

OPERATOR_TOKEN = "op-secret-token-for-tests"


def _bundle(key_id: str, ts: int) -> dict:
    return {
        "suite": "x25519-mlkem768",
        "hybrid_public_hex": "00" * 16 + key_id,
        "key_id": key_id,
        "device_id": f"dev-{key_id}",
        "last_published": ts,
    }


@pytest.fixture()
def pq(tmp_path, monkeypatch):
    """pq_prekeys bound to an isolated SKCHAT_HOME."""
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))
    from skchat import pq_prekeys

    importlib.reload(pq_prekeys)
    return pq_prekeys


@pytest.fixture()
def client(monkeypatch):
    # An operator token is configured: only callers presenting it are operators,
    # so a plain (tokenless) request is a deterministic non-operator rejection.
    monkeypatch.setenv("SKCHAT_GUEST_OPERATOR_TOKEN", OPERATOR_TOKEN)

    from skchat import daemon_proxy

    app = FastAPI()
    app.include_router(daemon_proxy.router)
    return TestClient(app)


def test_operator_revoke_removes_the_slot(pq, client):
    pq.store_peer_bundle("chef", _bundle("aaaaaaaaaaaaaaaa", ts=1))
    pq.store_peer_bundle("chef", _bundle("bbbbbbbbbbbbbbbb", ts=2))

    r = client.request(
        "DELETE",
        "/api/v1/prekey/chef/aaaaaaaaaaaaaaaa",
        headers={"X-Operator-Token": OPERATOR_TOKEN},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["removed"] is True

    kids = {b["key_id"] for b in pq.load_peer_bundles("chef")}
    assert kids == {"bbbbbbbbbbbbbbbb"}


def test_non_operator_is_rejected_and_slot_survives(pq, client):
    pq.store_peer_bundle("chef", _bundle("aaaaaaaaaaaaaaaa", ts=1))

    r = client.request("DELETE", "/api/v1/prekey/chef/aaaaaaaaaaaaaaaa")
    assert r.status_code in (401, 403), r.text

    # The slot must be untouched by an unauthorized caller.
    kids = {b["key_id"] for b in pq.load_peer_bundles("chef")}
    assert kids == {"aaaaaaaaaaaaaaaa"}


def test_operator_revoke_missing_slot_reports_not_removed(pq, client):
    r = client.request(
        "DELETE",
        "/api/v1/prekey/chef/ffffffffffffffff",
        headers={"X-Operator-Token": OPERATOR_TOKEN},
    )
    assert r.status_code == 200, r.text
    assert r.json()["removed"] is False
