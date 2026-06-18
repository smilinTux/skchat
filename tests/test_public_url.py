"""Public-aware SFU endpoint selection (coord df42e2a4).

The browser must be handed a *public/Funnel-reachable* wss URL when a request
arrives over the public host, but the *tailnet* URL (:8443) for tailnet-origin
requests. Detection is by Host / X-Forwarded-Host header vs the tailnet host.

Backward compatibility: with ``SKCHAT_LIVEKIT_PUBLIC_URL`` unset, every path
returns the tailnet URL — identical to pre-public behavior.

Covers all three URL-handing sites:
  * GET  /livekit/config        (livekit_routes.py)
  * POST /livekit/token         (livekit_routes.py)
  * POST /guest/join            (guest.py)
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from skchat.livekit_routes import (  # noqa: E402
    public_aware_livekit_url,
    register_livekit_routes,
)

_TAILNET_URL = "wss://noroc2027.tail204f0c.ts.net:8443"
_TAILNET_HOST = "noroc2027.tail204f0c.ts.net"
_PUBLIC_URL = "wss://sfu.skworld.io"
_PUBLIC_HOST = "noroc2027.funnel.skworld.io"


class _FakeRequest:
    """Minimal stand-in for starlette.Request exposing only ``.headers``."""

    def __init__(self, headers: dict[str, str] | None = None):
        # Lowercase keys to mimic starlette's case-insensitive header map.
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}


# -- Unit: the helper ---------------------------------------------------------


def test_helper_no_public_url_returns_tailnet(monkeypatch):
    """No public URL set -> always tailnet, regardless of Host (unchanged)."""
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", _TAILNET_URL)
    monkeypatch.delenv("SKCHAT_LIVEKIT_PUBLIC_URL", raising=False)
    # Even a public-looking host gets the tailnet URL when no public URL is set.
    req = _FakeRequest({"Host": _PUBLIC_HOST})
    assert public_aware_livekit_url(req) == _TAILNET_URL


def test_helper_public_host_returns_public(monkeypatch):
    """Public URL set + public Host -> public URL."""
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", _TAILNET_URL)
    monkeypatch.setenv("SKCHAT_LIVEKIT_PUBLIC_URL", _PUBLIC_URL)
    req = _FakeRequest({"Host": _PUBLIC_HOST})
    assert public_aware_livekit_url(req) == _PUBLIC_URL


def test_helper_tailnet_host_returns_tailnet(monkeypatch):
    """Public URL set + tailnet Host -> tailnet URL."""
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", _TAILNET_URL)
    monkeypatch.setenv("SKCHAT_LIVEKIT_PUBLIC_URL", _PUBLIC_URL)
    req = _FakeRequest({"Host": f"{_TAILNET_HOST}:8443"})
    assert public_aware_livekit_url(req) == _TAILNET_URL


def test_helper_x_forwarded_host_wins(monkeypatch):
    """Funnel sets X-Forwarded-Host; it takes precedence over Host."""
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", _TAILNET_URL)
    monkeypatch.setenv("SKCHAT_LIVEKIT_PUBLIC_URL", _PUBLIC_URL)
    req = _FakeRequest({"X-Forwarded-Host": _PUBLIC_HOST, "Host": _TAILNET_HOST})
    assert public_aware_livekit_url(req) == _PUBLIC_URL


def test_helper_no_host_header_returns_tailnet(monkeypatch):
    """No host header (local/tailnet caller) -> tailnet URL even with public set."""
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", _TAILNET_URL)
    monkeypatch.setenv("SKCHAT_LIVEKIT_PUBLIC_URL", _PUBLIC_URL)
    assert public_aware_livekit_url(_FakeRequest()) == _TAILNET_URL


def test_helper_explicit_tailnet_host_env(monkeypatch):
    """SKCHAT_TAILNET_HOST overrides the host derived from the lk url."""
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", "wss://internal-name:8443")
    monkeypatch.setenv("SKCHAT_LIVEKIT_PUBLIC_URL", _PUBLIC_URL)
    monkeypatch.setenv("SKCHAT_TAILNET_HOST", _TAILNET_HOST)
    # A request on the explicit tailnet host -> tailnet url.
    assert (
        public_aware_livekit_url(_FakeRequest({"Host": _TAILNET_HOST}))
        == "wss://internal-name:8443"
    )
    # A request on some other host -> public url.
    assert public_aware_livekit_url(_FakeRequest({"Host": _PUBLIC_HOST})) == _PUBLIC_URL


# -- Integration: GET /livekit/config -----------------------------------------


def _lk_client() -> TestClient:
    app = FastAPI()
    register_livekit_routes(app)
    return TestClient(app)


def test_config_public_host_gets_public(monkeypatch):
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", _TAILNET_URL)
    monkeypatch.setenv("SKCHAT_LIVEKIT_PUBLIC_URL", _PUBLIC_URL)
    client = _lk_client()
    r = client.get("/livekit/config", headers={"Host": _PUBLIC_HOST})
    assert r.status_code == 200
    assert r.json()["url"] == _PUBLIC_URL


def test_config_tailnet_host_gets_tailnet(monkeypatch):
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", _TAILNET_URL)
    monkeypatch.setenv("SKCHAT_LIVEKIT_PUBLIC_URL", _PUBLIC_URL)
    client = _lk_client()
    r = client.get("/livekit/config", headers={"Host": _TAILNET_HOST})
    assert r.status_code == 200
    assert r.json()["url"] == _TAILNET_URL


def test_config_no_public_set_always_tailnet(monkeypatch):
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", _TAILNET_URL)
    monkeypatch.delenv("SKCHAT_LIVEKIT_PUBLIC_URL", raising=False)
    client = _lk_client()
    r = client.get("/livekit/config", headers={"Host": _PUBLIC_HOST})
    assert r.status_code == 200
    assert r.json()["url"] == _TAILNET_URL


# -- Integration: POST /livekit/token -----------------------------------------
#
# Token minting needs livekit-api + creds. We assert the URL-selection path by
# checking the response (when available) OR the 503 hint (when livekit-api is
# absent), so the test does not hard-depend on the optional SFU library. The
# url-selection branch is fully covered by the helper + /livekit/config tests.


def test_token_url_is_public_aware_when_available(monkeypatch):
    pytest.importorskip("livekit")  # token mint requires livekit-api
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", _TAILNET_URL)
    monkeypatch.setenv("SKCHAT_LIVEKIT_PUBLIC_URL", _PUBLIC_URL)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "s")
    # /livekit/token is gated (loopback/tailnet OR operator token). A public-host
    # request (public X-Forwarded-Host) comes from a proxy, so it must carry an
    # operator token to pass the gate — that's the authorized-proxied-caller case
    # whose URL selection we're asserting here.
    monkeypatch.setenv("SKCHAT_GUEST_OPERATOR_TOKEN", "optok")
    _op = {"X-Operator-Token": "optok"}
    # Re-import so module-level creds reflect the patched env.
    import importlib

    import skchat.livekit_routes as lkr

    importlib.reload(lkr)
    app = FastAPI()
    lkr.register_livekit_routes(app)
    client = TestClient(app)

    r_pub = client.post(
        "/livekit/token", json={"identity": "alice"}, headers={"Host": _PUBLIC_HOST, **_op}
    )
    r_tail = client.post(
        "/livekit/token", json={"identity": "alice"}, headers={"Host": _TAILNET_HOST, **_op}
    )
    assert r_pub.status_code == 200 and r_pub.json()["url"] == _PUBLIC_URL
    assert r_tail.status_code == 200 and r_tail.json()["url"] == _TAILNET_URL
    # Restore the module to its env-default (no-creds) state for later tests:
    # clear the creds BEFORE reloading (monkeypatch reverts only at teardown, so
    # without this the reloaded module keeps API_KEY="k" cached and pollutes the
    # token-gate tests that expect a no-creds 503).
    monkeypatch.delenv("SKCHAT_LIVEKIT_API_KEY", raising=False)
    monkeypatch.delenv("SKCHAT_LIVEKIT_API_SECRET", raising=False)
    importlib.reload(lkr)


# -- Integration: POST /guest/join (guest.py) ---------------------------------


def _guest_client() -> TestClient:
    from skchat.guest import register_guest_routes

    app = FastAPI()
    register_guest_routes(app)
    return TestClient(app)


def _make_invite(room: str) -> str:
    from skchat.guest import InviteIssuer

    return InviteIssuer().create_invite(room=room, display="G", ttl=3600)["invite_token"]


def test_guest_join_public_host_gets_public_lk_url(monkeypatch):
    pytest.importorskip("livekit")  # /guest/join mints a real LiveKit token
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", _TAILNET_URL)
    monkeypatch.setenv("SKCHAT_LIVEKIT_PUBLIC_URL", _PUBLIC_URL)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "secret-0123456789")
    monkeypatch.setenv("SKCHAT_GUEST_TOKEN_SECRET", "guest-secret-xyz")
    client = _guest_client()
    token = _make_invite("room-public")
    r = client.post(
        "/guest/join",
        json={"room": "room-public", "invite_token": token, "display_name": "G"},
        headers={"Host": _PUBLIC_HOST},
    )
    assert r.status_code == 200, r.text
    assert r.json()["lk_url"] == _PUBLIC_URL


def test_guest_join_tailnet_host_gets_tailnet_lk_url(monkeypatch):
    pytest.importorskip("livekit")
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", _TAILNET_URL)
    monkeypatch.setenv("SKCHAT_LIVEKIT_PUBLIC_URL", _PUBLIC_URL)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "secret-0123456789")
    monkeypatch.setenv("SKCHAT_GUEST_TOKEN_SECRET", "guest-secret-xyz")
    client = _guest_client()
    token = _make_invite("room-tailnet")
    r = client.post(
        "/guest/join",
        json={"room": "room-tailnet", "invite_token": token, "display_name": "G"},
        headers={"Host": _TAILNET_HOST},
    )
    assert r.status_code == 200, r.text
    assert r.json()["lk_url"] == _TAILNET_URL


def test_guest_join_no_public_set_always_tailnet(monkeypatch):
    pytest.importorskip("livekit")
    monkeypatch.setenv("SKCHAT_LIVEKIT_URL", _TAILNET_URL)
    monkeypatch.delenv("SKCHAT_LIVEKIT_PUBLIC_URL", raising=False)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "secret-0123456789")
    monkeypatch.setenv("SKCHAT_GUEST_TOKEN_SECRET", "guest-secret-xyz")
    client = _guest_client()
    token = _make_invite("room-default")
    r = client.post(
        "/guest/join",
        json={"room": "room-default", "invite_token": token, "display_name": "G"},
        headers={"Host": _PUBLIC_HOST},
    )
    assert r.status_code == 200, r.text
    assert r.json()["lk_url"] == _TAILNET_URL
