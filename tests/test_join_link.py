"""Sovereign Conf Calls — the one-link, two-button join chooser (coord 31dae903).

``GET /join/{room}?invite=...`` no longer drops straight into the guest-only
join form: it now serves a *chooser* (``static/join.html``) that offers BOTH a
SOVEREIGN branch ("Join as Chef (sign in)" -> POST /join/sovereign) and a GUEST
branch ("Join as guest" -> POST /guest/join). The invite secret authorizes
ENTRY; the identity choice is downstream.

These tests assert:
  * the chooser page returns 200 HTML carrying BOTH options + both POST seams,
  * the room name is HTML-escaped into the page (no XSS regression),
  * the legacy guest flow (POST /guest/join) is unchanged — a valid invite still
    mints a LiveKit token (with a stubbed token builder so no SFU is needed).
"""

from __future__ import annotations

import time

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from skchat import guest as guest_mod  # noqa: E402
from skchat.guest import InviteIssuer, register_guest_routes  # noqa: E402

_SECRET = "test-join-link-secret-do-not-use"
_ROOM = "lumina-and-chef"


def _app(monkeypatch) -> FastAPI:
    monkeypatch.setenv("SKCHAT_GUEST_TOKEN_SECRET", _SECRET)
    monkeypatch.delenv("SKCHAT_GUEST_OPERATOR_TOKEN", raising=False)
    app = FastAPI()
    register_guest_routes(app)
    return app


# ── Chooser: one link, two branches ──────────────────────────────────────────


def test_join_chooser_offers_both_options(monkeypatch):
    """GET /join/{room}?invite=... → 200 HTML with BOTH a sovereign and a guest
    option, each wired to its respective POST seam."""
    app = _app(monkeypatch)
    c = TestClient(app, client=("8.8.8.8", 12345))  # public client allowed here
    r = c.get(f"/join/{_ROOM}", params={"invite": "tok-abc"})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text

    # Sovereign option present + points at the sovereign-join seam.
    assert "sovereign-option" in body
    assert "/join/sovereign" in body
    assert "sign in" in body.lower()

    # Guest option present + points at the unchanged guest-join seam.
    assert "guest-option" in body
    assert "/guest/join" in body
    assert "guest" in body.lower()

    # Invite secret is carried into the page (entry authorization) for both
    # branches; the room name is substituted.
    assert "tok-abc" in body
    assert _ROOM in body


def test_join_chooser_escapes_room_name(monkeypatch):
    """The room name is HTML-escaped into the chooser (no stored-XSS via the
    path segment). Uses a distinctive payload so the page's own (legitimate)
    <script> block does not mask the check."""
    app = _app(monkeypatch)
    c = TestClient(app, client=("127.0.0.1", 12345))
    r = c.get("/join/<xss>'\"", params={"invite": "x"})
    assert r.status_code == 200
    # The raw injected sequence must NOT survive verbatim...
    assert "<xss>" not in r.text
    # ...it must be HTML-escaped instead.
    assert "&lt;xss&gt;" in r.text


def test_join_missing_invite_is_rejected(monkeypatch):
    """No invite param → 400 (unchanged behavior)."""
    app = _app(monkeypatch)
    c = TestClient(app, client=("127.0.0.1", 12345))
    r = c.get(f"/join/{_ROOM}")
    assert r.status_code == 400


# ── Guest branch: no regression on POST /guest/join ───────────────────────────


def test_guest_join_still_works(monkeypatch):
    """A valid invite still mints a LiveKit token via POST /guest/join. The
    token builder is stubbed so the test needs no livekit-api / SFU; this
    asserts the guest seam itself is unchanged by the chooser."""
    monkeypatch.setenv("SKCHAT_GUEST_TOKEN_SECRET", _SECRET)
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "dummy-key")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "dummy-secret")
    monkeypatch.setattr(
        guest_mod,
        "build_livekit_token",
        lambda guest, **_kw: f"LK({guest.identity})",
    )

    issuer = InviteIssuer(secret=_SECRET)
    invite = issuer.create_invite(_ROOM, display="hint")["invite_token"]

    app = FastAPI()
    register_guest_routes(app)
    c = TestClient(app, client=("127.0.0.1", 12345))

    r = c.post(
        "/guest/join",
        json={"room": _ROOM, "invite_token": invite, "display_name": "Alice"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["room"] == _ROOM
    assert data["display"] == "Alice"
    # Server-assigned guest identity (guest cannot choose it).
    assert data["identity"].startswith("guest:")
    assert data["lk_token"] == f"LK({data['identity']})"
    assert data["expires_at"] > time.time()


def test_guest_join_rejects_bad_invite(monkeypatch):
    """An invalid invite token is still rejected with a generic 401."""
    monkeypatch.setenv("SKCHAT_GUEST_TOKEN_SECRET", _SECRET)
    app = FastAPI()
    register_guest_routes(app)
    c = TestClient(app, client=("127.0.0.1", 12345))
    r = c.post(
        "/guest/join",
        json={"room": _ROOM, "invite_token": "not-a-real-jwt", "display_name": "Eve"},
    )
    assert r.status_code == 401
