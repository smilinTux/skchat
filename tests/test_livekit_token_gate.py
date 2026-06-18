"""POST /livekit/token is gated to loopback/tailnet OR an operator token.

Covers coord task 11f3aec0 (Sovereign Conf Calls): ``/livekit/token`` mints a
FULL-publish LiveKit JWT for ANY caller-supplied ``identity`` — an impersonation
hole if reachable anonymously over Tailscale Funnel. The gate (mirroring the
guest operator gate in ``skchat.guest._require_operator``) allows:

  * loopback (127.0.0.1 / ::1) — the path lumina-call.py relies on,
  * private/tailnet IPs (RFC1918 + 100.64.0.0/10 CGNAT),
  * any client presenting a valid ``SKCHAT_GUEST_OPERATOR_TOKEN``,

and rejects everything else (public/Funnel callers) with 401/403 BEFORE any
token is minted.

These tests build a fresh FastAPI app via ``register_livekit_routes`` and use
TestClient with a controlled client host (ASGI scope) to assert the gate. They
never reach ``_mint_token`` for the allowed cases — a 503 ("livekit not
configured") is the *expected* success signal: it proves the gate let the
request through to the (un-credentialed) mint logic.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from skchat.livekit_routes import register_livekit_routes  # noqa: E402


def _client(app: FastAPI, *, host: str) -> TestClient:
    # TestClient lets us control the perceived client host via the ASGI scope,
    # mirroring tests/test_guest_routes_wired.py.
    return TestClient(app, client=(host, 12345))


def _app() -> FastAPI:
    app = FastAPI()
    register_livekit_routes(app)
    return app


# A request that PASSES the gate but has no LiveKit creds returns 503; a request
# REJECTED by the gate returns 401/403. We assert on those status codes — the
# 503 is the "gate allowed me through" signal (no creds wired in tests).
_GATE_ALLOWED = 503
_GATE_REJECTED = {401, 403}


def test_loopback_caller_passes_gate(monkeypatch):
    """127.0.0.1 (lumina-call.py path) is allowed through the gate."""
    monkeypatch.delenv("SKCHAT_GUEST_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("SKCHAT_LIVEKIT_API_KEY", raising=False)
    monkeypatch.delenv("SKCHAT_LIVEKIT_API_SECRET", raising=False)
    c = _client(_app(), host="127.0.0.1")
    r = c.post("/livekit/token", json={"identity": "lumina"})
    assert r.status_code == _GATE_ALLOWED, r.text
    assert "token" not in r.json() or r.json().get("token") is None


def test_tailnet_caller_passes_gate(monkeypatch):
    """A Tailscale CGNAT 100.x client is allowed through the gate."""
    monkeypatch.delenv("SKCHAT_GUEST_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("SKCHAT_LIVEKIT_API_KEY", raising=False)
    monkeypatch.delenv("SKCHAT_LIVEKIT_API_SECRET", raising=False)
    c = _client(_app(), host="100.101.102.103")
    r = c.post("/livekit/token", json={"identity": "chef"})
    assert r.status_code == _GATE_ALLOWED, r.text


def test_rfc1918_caller_passes_gate(monkeypatch):
    """A private 192.168.x LAN client is allowed through the gate."""
    monkeypatch.delenv("SKCHAT_GUEST_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("SKCHAT_LIVEKIT_API_KEY", raising=False)
    monkeypatch.delenv("SKCHAT_LIVEKIT_API_SECRET", raising=False)
    c = _client(_app(), host="192.168.0.158")
    r = c.post("/livekit/token", json={"identity": "lumina"})
    assert r.status_code == _GATE_ALLOWED, r.text


def test_public_caller_without_token_rejected(monkeypatch):
    """A public (non-private) client with no operator token -> 403, no mint."""
    monkeypatch.delenv("SKCHAT_GUEST_OPERATOR_TOKEN", raising=False)
    c = _client(_app(), host="8.8.8.8")
    r = c.post("/livekit/token", json={"identity": "lumina"})
    assert r.status_code in _GATE_REJECTED, r.text
    assert "token" not in r.text


def test_public_caller_with_valid_operator_token_allowed(monkeypatch):
    """With SKCHAT_GUEST_OPERATOR_TOKEN set, a public caller presenting it passes."""
    monkeypatch.setenv("SKCHAT_GUEST_OPERATOR_TOKEN", "op-token-xyz")
    monkeypatch.delenv("SKCHAT_LIVEKIT_API_KEY", raising=False)
    monkeypatch.delenv("SKCHAT_LIVEKIT_API_SECRET", raising=False)
    c = _client(_app(), host="8.8.8.8")
    # Wrong/missing token -> 401 (operator auth required).
    assert c.post("/livekit/token", json={"identity": "x"}).status_code == 401
    # Correct token -> passes the gate (then 503: no creds).
    r = c.post(
        "/livekit/token",
        json={"identity": "x"},
        headers={"Authorization": "Bearer op-token-xyz"},
    )
    assert r.status_code == _GATE_ALLOWED, r.text


def test_public_caller_with_x_operator_token_header(monkeypatch):
    """X-Operator-Token header is also accepted (same as the guest gate)."""
    monkeypatch.setenv("SKCHAT_GUEST_OPERATOR_TOKEN", "op-token-xyz")
    monkeypatch.delenv("SKCHAT_LIVEKIT_API_KEY", raising=False)
    monkeypatch.delenv("SKCHAT_LIVEKIT_API_SECRET", raising=False)
    c = _client(_app(), host="203.0.113.9")
    r = c.post(
        "/livekit/token",
        json={"identity": "x"},
        headers={"X-Operator-Token": "op-token-xyz"},
    )
    assert r.status_code == _GATE_ALLOWED, r.text
