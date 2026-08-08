"""Decrypt-failure NACK: fast-recovery re-pull trigger (coord 4c054eab, crit 1).

Criteria 2+3 (the 6h TTL re-pull, ``_maybe_refresh_peer``) are already merged.
This is criterion 1: the *fast* path. When a recipient device receives a
pqdm2/pqdm1 DM it cannot open (no slot for its key_id, or the AEAD open fails),
it POSTs ``/api/v1/dm/decrypt-failed`` to the SENDER's daemon. The sender then
re-pulls the REPORTING peer's freshly-republished bundle immediately, instead of
waiting for the TTL, so the next reply is sealed to the fresh slot.

Direction: the recipient (e.g. ``chef``) failed to open a message from the
sender (e.g. ``lumina``). The recipient reports to the sender's daemon so the
SENDER re-pulls the RECIPIENT's own bundle (the recipient just republished a
fresh slot the sender's cache is missing). So ``peer`` in the report is the
REPORTING device, and the endpoint re-pulls that peer's bundle.

Contract exercised here:

* An operator POST with ``{peer}`` forces a ``fetch_peer_prekey`` re-pull for
  the reporting peer, bypassing the TTL staleness gate. Returns ``{ok, refreshed}``.
* A non-operator caller is rejected (401/403) and nothing is re-pulled.
* Idempotent + rate-limited: a repeat NACK for the same peer inside the window
  collapses to a single re-pull (``refreshed`` False on the collapsed call).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

OPERATOR_TOKEN = "op-secret-token-for-tests"


@pytest.fixture()
def client(monkeypatch):
    # An operator token is configured: only callers presenting it are operators,
    # so a plain (tokenless) request is a deterministic non-operator rejection.
    monkeypatch.setenv("SKCHAT_GUEST_OPERATOR_TOKEN", OPERATOR_TOKEN)

    from skchat import daemon_proxy

    daemon_proxy._last_nack_repull.clear()
    daemon_proxy._last_refresh_attempt.clear()

    app = FastAPI()
    app.include_router(daemon_proxy.router)
    return TestClient(app)


def test_operator_report_triggers_repull_for_reporting_peer(client, monkeypatch):
    from skchat import daemon_proxy, prekey_exchange

    calls: list[str] = []

    def fake_fetch(peer_fqid, **kwargs):
        calls.append(peer_fqid)
        return None

    monkeypatch.setattr(prekey_exchange, "fetch_peer_prekey", fake_fetch)

    r = client.post(
        "/api/v1/dm/decrypt-failed",
        json={"peer": "chef", "message_id": "m-123"},
        headers={"X-Operator-Token": OPERATOR_TOKEN},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["refreshed"] is True
    # The reporting peer's bundle (chef) is what gets re-pulled.
    assert calls == ["chef"]
    # The TTL throttle for that peer is cleared so the next seal also refreshes.
    assert "chef" not in daemon_proxy._last_refresh_attempt


def test_repull_bypasses_ttl_staleness(client, monkeypatch):
    """Even a FRESH local bundle re-pulls on an explicit NACK (unlike the TTL path
    which only re-pulls a stale copy)."""
    from skchat import daemon_proxy, prekey_exchange

    calls: list[str] = []
    monkeypatch.setattr(prekey_exchange, "fetch_peer_prekey", lambda p, **k: calls.append(p))
    # Force "not stale": the TTL gate would skip, the NACK must not.
    monkeypatch.setattr(daemon_proxy, "_peer_bundle_is_stale", lambda *a, **k: False)

    r = client.post(
        "/api/v1/dm/decrypt-failed",
        json={"peer": "chef"},
        headers={"X-Operator-Token": OPERATOR_TOKEN},
    )
    assert r.status_code == 200, r.text
    assert calls == ["chef"], "an explicit NACK must re-pull regardless of TTL freshness"


def test_non_operator_is_rejected_and_no_repull(client, monkeypatch):
    from skchat import prekey_exchange

    calls: list[str] = []
    monkeypatch.setattr(prekey_exchange, "fetch_peer_prekey", lambda p, **k: calls.append(p))

    r = client.post("/api/v1/dm/decrypt-failed", json={"peer": "chef"})
    assert r.status_code in (401, 403), r.text
    assert not calls, "an unauthorized NACK must not trigger a re-pull"


def test_idempotent_within_window_collapses_to_one_repull(client, monkeypatch):
    from skchat import prekey_exchange

    calls: list[str] = []
    monkeypatch.setattr(prekey_exchange, "fetch_peer_prekey", lambda p, **k: calls.append(p))

    first = client.post(
        "/api/v1/dm/decrypt-failed",
        json={"peer": "chef"},
        headers={"X-Operator-Token": OPERATOR_TOKEN},
    )
    second = client.post(
        "/api/v1/dm/decrypt-failed",
        json={"peer": "chef"},
        headers={"X-Operator-Token": OPERATOR_TOKEN},
    )
    assert first.json()["refreshed"] is True
    assert second.json()["refreshed"] is False, "repeat NACK in-window must collapse"
    assert calls == ["chef"], "the re-pull must fire exactly once inside the window"


def test_missing_peer_is_a_400(client):
    r = client.post(
        "/api/v1/dm/decrypt-failed",
        json={"message_id": "m-1"},
        headers={"X-Operator-Token": OPERATOR_TOKEN},
    )
    assert r.status_code == 400, r.text


def test_repull_that_raises_is_swallowed(client, monkeypatch):
    """A failed re-pull must not 500 the NACK; it is best-effort."""
    from skchat import prekey_exchange

    def boom(peer_fqid, **kwargs):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(prekey_exchange, "fetch_peer_prekey", boom)

    r = client.post(
        "/api/v1/dm/decrypt-failed",
        json={"peer": "chef"},
        headers={"X-Operator-Token": OPERATOR_TOKEN},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
