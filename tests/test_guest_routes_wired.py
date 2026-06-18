"""Guest routes are wired into the webui app + operator-gated correctly.

Covers coord task 9edc0b1e (Sovereign Conf Calls): ``register_guest_routes`` is
now called from ``skchat.webui`` so ``/guest/invite``, ``/join/{room}``,
``/guest/join`` and ``/guest/revoke`` are no longer 404, AND the two
operator-only endpoints reject anonymous/public callers.

These tests build a fresh FastAPI app via ``register_guest_routes`` (so they
exercise THIS source tree's code) and also assert the routes are present on the
shared ``skchat.webui.app`` (the actual wiring point).
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from skchat.guest import register_guest_routes  # noqa: E402

_SECRET = "test-guest-secret-do-not-use"


def _route_paths(app) -> set[str]:
    return {r.path for r in app.routes}


# -- Wiring: routes are registered --------------------------------------------


def test_guest_routes_registered_on_fresh_app(monkeypatch):
    monkeypatch.setenv("SKCHAT_GUEST_TOKEN_SECRET", _SECRET)
    app = FastAPI()
    register_guest_routes(app)
    paths = _route_paths(app)
    assert "/join/{room}" in paths
    assert "/guest/join" in paths
    assert "/guest/invite" in paths
    assert "/guest/revoke/{jti}" in paths


def test_guest_routes_registered_on_webui_app():
    # The real wiring point: importing the webui module must register the routes
    # on its module-level `app` (this is what was missing -- they used to 404).
    from skchat.webui import app

    paths = _route_paths(app)
    assert "/join/{room}" in paths
    assert "/guest/join" in paths
    assert "/guest/invite" in paths


# -- Operator-auth gate on /guest/invite + /guest/revoke ----------------------


def _client(app, *, host: str) -> TestClient:
    # TestClient lets us control the perceived client host via the ASGI scope.
    return TestClient(app, client=(host, 12345))


def test_guest_invite_rejects_public_caller(monkeypatch):
    """No operator token set + public (non-private) client IP -> 403, no invite."""
    monkeypatch.setenv("SKCHAT_GUEST_TOKEN_SECRET", _SECRET)
    monkeypatch.delenv("SKCHAT_GUEST_OPERATOR_TOKEN", raising=False)
    app = FastAPI()
    register_guest_routes(app)
    c = _client(app, host="8.8.8.8")  # public IP -- not loopback/tailnet
    r = c.post("/guest/invite", json={"room": "lumina-and-chef"})
    assert r.status_code == 403
    assert "invite_token" not in r.text


def test_guest_revoke_rejects_public_caller(monkeypatch):
    monkeypatch.setenv("SKCHAT_GUEST_TOKEN_SECRET", _SECRET)
    monkeypatch.delenv("SKCHAT_GUEST_OPERATOR_TOKEN", raising=False)
    app = FastAPI()
    register_guest_routes(app)
    c = _client(app, host="203.0.113.7")  # public IP
    r = c.request("DELETE", "/guest/revoke/deadbeef")
    assert r.status_code == 403


def test_guest_invite_allows_loopback_caller(monkeypatch):
    """Loopback/tailnet client is trusted as operator when no token is set."""
    monkeypatch.setenv("SKCHAT_GUEST_TOKEN_SECRET", _SECRET)
    monkeypatch.delenv("SKCHAT_GUEST_OPERATOR_TOKEN", raising=False)
    app = FastAPI()
    register_guest_routes(app)
    c = _client(app, host="127.0.0.1")
    r = c.post("/guest/invite", json={"room": "lumina-and-chef"})
    assert r.status_code == 200
    body = r.json()
    assert body["room"] == "lumina-and-chef"
    assert body["invite_token"]


def test_guest_invite_allows_bearer_token_from_public(monkeypatch):
    """With a shared operator token set, a public caller presenting it is allowed."""
    monkeypatch.setenv("SKCHAT_GUEST_TOKEN_SECRET", _SECRET)
    monkeypatch.setenv("SKCHAT_GUEST_OPERATOR_TOKEN", "op-token-123")
    app = FastAPI()
    register_guest_routes(app)
    c = _client(app, host="8.8.8.8")
    # Wrong/missing token -> 401.
    assert c.post("/guest/invite", json={"room": "r"}).status_code == 401
    # Correct token -> 200.
    r = c.post(
        "/guest/invite",
        json={"room": "r"},
        headers={"Authorization": "Bearer op-token-123"},
    )
    assert r.status_code == 200
    assert r.json()["invite_token"]


def test_guest_join_page_is_public(monkeypatch):
    """/join/{room} stays public (invite-JWT-gated), reachable without operator auth."""
    monkeypatch.setenv("SKCHAT_GUEST_TOKEN_SECRET", _SECRET)
    monkeypatch.delenv("SKCHAT_GUEST_OPERATOR_TOKEN", raising=False)
    app = FastAPI()
    register_guest_routes(app)
    c = _client(app, host="8.8.8.8")  # public client allowed here
    r = c.get("/join/lumina-and-chef", params={"invite": "anything"})
    assert r.status_code == 200
    assert "Join" in r.text
