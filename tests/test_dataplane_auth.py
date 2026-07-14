"""Tests for the fail-closed CapAuth data-plane gate (P0.5 / SEAM 7).

The chat data plane (POST /api/send, POST /api/v1/prekey, GET /api/v1/inbox)
ships without authentication. ``skchat.dataplane_auth`` adds an OPT-IN CapAuth
gate behind ``SKCHAT_DATAPLANE_AUTH`` (default OFF):

  * flag OFF (default) -> endpoints behave exactly as before (no token needed).
  * flag ON  + missing/invalid capauth -> 401.
  * flag ON  + valid capauth           -> passes through unchanged.

The real capauth verifier is never touched here — a stub ``CapAuthValidator``
(same ``validate`` surface) is injected via ``set_validator``.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from skchat import daemon_proxy, dataplane_auth, webui


# --------------------------------------------------------------------------- #
# Test doubles + fixtures
# --------------------------------------------------------------------------- #
class _StubValidator:
    """CapAuthValidator stand-in: records tokens, returns a fixed verdict."""

    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.seen: list[str] = []

    def validate(self, token: str) -> bool:
        self.seen.append(token)
        return self.ok


@pytest.fixture(autouse=True)
def _reset_validator():
    """Never leak an injected validator across tests."""
    yield
    dataplane_auth.set_validator(None)


def _enable(monkeypatch, *, ok: bool) -> _StubValidator:
    """Turn the gate on and inject a stub validator; return the stub."""
    monkeypatch.setenv("SKCHAT_DATAPLANE_AUTH", "1")
    stub = _StubValidator(ok)
    dataplane_auth.set_validator(stub)
    return stub


# --------------------------------------------------------------------------- #
# Unit: flag parsing
# --------------------------------------------------------------------------- #
def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("SKCHAT_DATAPLANE_AUTH", raising=False)
    assert dataplane_auth.dataplane_auth_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", " On "])
def test_flag_truthy_values_enable(monkeypatch, val):
    monkeypatch.setenv("SKCHAT_DATAPLANE_AUTH", val)
    assert dataplane_auth.dataplane_auth_enabled() is True


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "nope"])
def test_flag_non_truthy_values_stay_off(monkeypatch, val):
    monkeypatch.setenv("SKCHAT_DATAPLANE_AUTH", val)
    assert dataplane_auth.dataplane_auth_enabled() is False


# --------------------------------------------------------------------------- #
# Unit: enforce_dataplane_auth on a bare Request
# --------------------------------------------------------------------------- #
def _fake_request(headers: dict[str, str] | None = None) -> Request:
    raw = [
        (k.lower().encode(), v.encode())
        for k, v in (headers or {}).items()
    ]
    return Request({"type": "http", "headers": raw})


def test_enforce_noop_when_flag_off(monkeypatch):
    monkeypatch.delenv("SKCHAT_DATAPLANE_AUTH", raising=False)
    stub = _StubValidator(False)
    dataplane_auth.set_validator(stub)
    # No exception even with no credential — the gate is off.
    dataplane_auth.enforce_dataplane_auth(_fake_request())
    assert stub.seen == []  # validator never consulted when disabled


def test_enforce_401_when_flag_on_and_no_credential(monkeypatch):
    _enable(monkeypatch, ok=True)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        dataplane_auth.enforce_dataplane_auth(_fake_request())
    assert exc.value.status_code == 401


def test_enforce_401_when_flag_on_and_invalid_credential(monkeypatch):
    stub = _enable(monkeypatch, ok=False)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        dataplane_auth.enforce_dataplane_auth(
            _fake_request({"authorization": "CapAuth deadbeef"})
        )
    assert exc.value.status_code == 401
    assert stub.seen == ["deadbeef"]


def test_enforce_passes_when_flag_on_and_valid_credential(monkeypatch):
    stub = _enable(monkeypatch, ok=True)
    dataplane_auth.enforce_dataplane_auth(
        _fake_request({"authorization": "CapAuth good-token"})
    )
    assert stub.seen == ["good-token"]


def test_extract_credential_supports_bearer_and_header(monkeypatch):
    stub = _enable(monkeypatch, ok=True)
    dataplane_auth.enforce_dataplane_auth(
        _fake_request({"authorization": "Bearer bt"})
    )
    dataplane_auth.enforce_dataplane_auth(_fake_request({"x-capauth-token": "ht"}))
    assert stub.seen == ["bt", "ht"]


# --------------------------------------------------------------------------- #
# Integration: webui POST /api/send
# --------------------------------------------------------------------------- #
class _StubTransport:
    def __init__(self):
        self.calls: list[dict] = []

    def send_and_store(self, recipient: str, content: str, **kw):
        self.calls.append({"recipient": recipient, "content": content})
        return {"delivered": True, "message_id": "x", "recipient": recipient}


def _patch_webui(monkeypatch):
    transport = _StubTransport()
    monkeypatch.setattr(webui, "_get_identity", lambda: "capauth:tester@local")
    monkeypatch.setattr(webui, "_get_transport", lambda identity: transport)

    async def _fake_broadcast(msg_dict):
        return None

    monkeypatch.setattr(webui, "_ws_broadcast", _fake_broadcast)
    return transport


def test_api_send_flag_off_unchanged(monkeypatch):
    monkeypatch.delenv("SKCHAT_DATAPLANE_AUTH", raising=False)
    transport = _patch_webui(monkeypatch)
    r = TestClient(webui.app).post(
        "/api/send", json={"recipient": "capauth:bob@local", "content": "hi"}
    )
    assert r.status_code == 200
    assert transport.calls == [{"recipient": "capauth:bob@local", "content": "hi"}]


def test_api_send_flag_on_missing_capauth_is_401(monkeypatch):
    _enable(monkeypatch, ok=False)
    transport = _patch_webui(monkeypatch)
    r = TestClient(webui.app).post(
        "/api/send", json={"recipient": "capauth:bob@local", "content": "hi"}
    )
    assert r.status_code == 401
    assert transport.calls == []  # never reached the transport path


def test_api_send_flag_on_invalid_capauth_is_401(monkeypatch):
    _enable(monkeypatch, ok=False)
    transport = _patch_webui(monkeypatch)
    r = TestClient(webui.app).post(
        "/api/send",
        json={"recipient": "capauth:bob@local", "content": "hi"},
        headers={"Authorization": "CapAuth bad"},
    )
    assert r.status_code == 401
    assert transport.calls == []


def test_api_send_flag_on_valid_capauth_passes(monkeypatch):
    _enable(monkeypatch, ok=True)
    transport = _patch_webui(monkeypatch)
    r = TestClient(webui.app).post(
        "/api/send",
        json={"recipient": "capauth:bob@local", "content": "hi"},
        headers={"Authorization": "CapAuth good"},
    )
    assert r.status_code == 200
    assert transport.calls == [{"recipient": "capauth:bob@local", "content": "hi"}]


# --------------------------------------------------------------------------- #
# Integration: daemon_proxy POST /api/v1/prekey + GET /api/v1/inbox
# --------------------------------------------------------------------------- #
@pytest.fixture
def proxy_client(monkeypatch):
    # No real prekey store / history writes in tests.
    from skchat import pq_prekeys

    monkeypatch.setattr(daemon_proxy, "_lumina_messages", lambda limit=500: [])
    monkeypatch.setattr(pq_prekeys, "store_peer_bundle", lambda peer, bundle: None)
    monkeypatch.setattr(pq_prekeys, "peer_is_hybrid", lambda peer: False)

    app = FastAPI()
    app.include_router(daemon_proxy.router)
    return TestClient(app)


def test_prekey_flag_off_unchanged(monkeypatch, proxy_client):
    monkeypatch.delenv("SKCHAT_DATAPLANE_AUTH", raising=False)
    r = proxy_client.post("/api/v1/prekey", json={"owner": "chef", "suite": "x"})
    assert r.status_code == 200


def test_prekey_flag_on_missing_capauth_is_401(monkeypatch, proxy_client):
    _enable(monkeypatch, ok=False)
    r = proxy_client.post("/api/v1/prekey", json={"owner": "chef", "suite": "x"})
    assert r.status_code == 401


def test_prekey_flag_on_valid_capauth_passes(monkeypatch, proxy_client):
    _enable(monkeypatch, ok=True)
    r = proxy_client.post(
        "/api/v1/prekey",
        json={"owner": "chef", "suite": "x"},
        headers={"Authorization": "CapAuth good"},
    )
    assert r.status_code == 200


def test_inbox_flag_off_unchanged(monkeypatch, proxy_client):
    monkeypatch.delenv("SKCHAT_DATAPLANE_AUTH", raising=False)
    r = proxy_client.get("/api/v1/inbox")
    assert r.status_code == 200
    assert r.json() == {"messages": []}


def test_inbox_flag_on_missing_capauth_is_401(monkeypatch, proxy_client):
    _enable(monkeypatch, ok=False)
    r = proxy_client.get("/api/v1/inbox")
    assert r.status_code == 401


def test_inbox_flag_on_valid_capauth_passes(monkeypatch, proxy_client):
    _enable(monkeypatch, ok=True)
    r = proxy_client.get("/api/v1/inbox", headers={"Authorization": "CapAuth good"})
    assert r.status_code == 200
    assert r.json() == {"messages": []}
