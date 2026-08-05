"""Tests for iframe-friendly, module-scoped EMBED tokens (embed_auth + proxies).

The shell's Grade B panes (``/skdashboard``, ``/skos``) are iframes that cannot set
an ``Authorization`` header, so once those proxies are gated they can only 401. The
authenticated app mints a short-lived, module-scoped, READ-ONLY embed token from
``POST /api/v1/embed-token`` and appends it to the iframe ``src`` as
``?embed_token=...``; the proxy accepts EITHER a full operator credential OR a valid
module-scoped embed token. An unauth request with no/invalid token still 401s.

These tests cover, per the re-enable plan:
  * mint endpoint: flag-off 404 (inert), unauth 401, authed 200.
  * proxy: no-token 401, valid Authorization 200-path, valid embed_token 200-path,
    expired/foreign-scope embed_token 401.
  * embed token is read-only (a POST that only presents an embed token is 403).

The proxy's upstream fetch is stubbed so no sibling daemon is required; we only
assert the AUTHORIZATION decision (200-path reached vs 401/403), never the
upstream body.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from skchat import dataplane_auth, embed_auth, webui


# --------------------------------------------------------------------------- #
# Helpers / fixtures
# --------------------------------------------------------------------------- #
class _FakeValidator:
    """Injectable dataplane validator: fixed verdict, records calls."""

    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.calls = 0

    def validate(self, token: str) -> bool:
        self.calls += 1
        return self.ok


@pytest.fixture
def client() -> TestClient:
    return TestClient(webui.app)


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    """Each test: known signing key, dataplane gate ON, mint flag off, default validator.

    The dataplane gate is ON so an unauthenticated proxy request 401s (the leak-
    closed state this feature is built against). Individual tests flip the mint
    flag / validator as needed.
    """
    monkeypatch.setenv("SKCHAT_EMBED_TOKEN_SECRET", "embed-test-secret")
    monkeypatch.setenv(dataplane_auth.ENV_FLAG, "1")  # SKCHAT_DATAPLANE_AUTH=1
    monkeypatch.delenv(embed_auth.EMBED_MINT_ENV_FLAG, raising=False)
    monkeypatch.delenv("SKCHAT_AUTHZ_PDP", raising=False)
    dataplane_auth.set_validator(None)
    yield
    dataplane_auth.set_validator(None)


@pytest.fixture
def stub_upstream(monkeypatch):
    """Stub the reverse proxy so a request that PASSES authorization returns 200.

    Lets us assert the authorization decision without a live skdashboard/skos.
    """
    from fastapi.responses import Response

    async def _fake_proxy(request, upstream, path, *, label, **kwargs):
        # Accept (and ignore) the real proxy's html_prefix / embed_token kwargs so
        # this stub tracks the signature without caring about body transforms.
        return Response(content=b"OK", status_code=200, media_type="text/plain")

    monkeypatch.setattr(webui, "_reverse_proxy", _fake_proxy)


# --------------------------------------------------------------------------- #
# Unit: mint + verify + scope
# --------------------------------------------------------------------------- #
class TestMintVerify:
    def test_mint_then_verify_roundtrip(self):
        token, exp = embed_auth.mint_embed_token("skdashboard")
        et = embed_auth.verify_embed_token(token, "skdashboard")
        assert et.module == "skdashboard"
        assert et.exp == exp

    def test_foreign_scope_rejected(self):
        token, _ = embed_auth.mint_embed_token("skos")
        with pytest.raises(embed_auth.EmbedAuthError):
            embed_auth.verify_embed_token(token, "skdashboard")

    def test_expired_rejected(self, monkeypatch):
        # Mint with a backdated clock so exp is already in the past, then verify
        # against the real clock (PyJWT enforces exp against real UTC time).
        real = time.time
        monkeypatch.setattr(time, "time", lambda: real() - 100)
        token, _ = embed_auth.mint_embed_token("skos", ttl=1)
        monkeypatch.setattr(time, "time", real)
        with pytest.raises(embed_auth.EmbedAuthError):
            embed_auth.verify_embed_token(token, "skos")

    def test_unknown_module_cannot_mint(self):
        with pytest.raises(embed_auth.EmbedAuthError):
            embed_auth.mint_embed_token("skchat")

    def test_wrong_key_rejected(self, monkeypatch):
        token, _ = embed_auth.mint_embed_token("skos")
        monkeypatch.setenv("SKCHAT_EMBED_TOKEN_SECRET", "different-secret")
        with pytest.raises(embed_auth.EmbedAuthError):
            embed_auth.verify_embed_token(token, "skos")

    def test_ttl_capped(self):
        _, exp = embed_auth.mint_embed_token("skos", ttl=99999)
        assert exp <= int(time.time()) + embed_auth.MAX_TTL + 1

    def test_operator_secret_derivation(self, monkeypatch):
        """With no explicit embed secret, the key derives from the operator secret."""
        monkeypatch.delenv("SKCHAT_EMBED_TOKEN_SECRET", raising=False)
        monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "op-secret")
        token, _ = embed_auth.mint_embed_token("skdashboard")
        assert embed_auth.verify_embed_token(token, "skdashboard").module == "skdashboard"

    def test_no_key_fails_closed(self, monkeypatch):
        monkeypatch.delenv("SKCHAT_EMBED_TOKEN_SECRET", raising=False)
        monkeypatch.delenv("SKCHAT_OPERATOR_TOKEN_SECRET", raising=False)
        with pytest.raises(embed_auth.EmbedAuthError):
            embed_auth.mint_embed_token("skos")


# --------------------------------------------------------------------------- #
# Mint endpoint gates
# --------------------------------------------------------------------------- #
class TestMintEndpoint:
    def test_flag_off_is_404_inert(self, client):
        dataplane_auth.set_validator(_FakeValidator(True))
        resp = client.post(
            "/api/v1/embed-token",
            headers={"Authorization": "Bearer valid"},
            json={"module": "skdashboard"},
        )
        assert resp.status_code == 404

    def test_flag_on_unauthenticated_is_401(self, client, monkeypatch):
        monkeypatch.setenv(embed_auth.EMBED_MINT_ENV_FLAG, "1")
        dataplane_auth.set_validator(_FakeValidator(False))
        resp = client.post("/api/v1/embed-token", json={"module": "skdashboard"})
        assert resp.status_code == 401

    def test_flag_on_authenticated_mints(self, client, monkeypatch):
        monkeypatch.setenv(embed_auth.EMBED_MINT_ENV_FLAG, "1")
        dataplane_auth.set_validator(_FakeValidator(True))
        resp = client.post(
            "/api/v1/embed-token",
            headers={"Authorization": "Bearer valid"},
            json={"module": "skos"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["module"] == "skos"
        assert body["expires_at"]
        # The minted token verifies for skos and NOT for skdashboard (scope).
        assert embed_auth.verify_embed_token(body["token"], "skos").module == "skos"
        with pytest.raises(embed_auth.EmbedAuthError):
            embed_auth.verify_embed_token(body["token"], "skdashboard")

    def test_bad_module_is_400(self, client, monkeypatch):
        monkeypatch.setenv(embed_auth.EMBED_MINT_ENV_FLAG, "1")
        dataplane_auth.set_validator(_FakeValidator(True))
        resp = client.post(
            "/api/v1/embed-token",
            headers={"Authorization": "Bearer valid"},
            json={"module": "skchat"},  # not an embeddable gated module
        )
        assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Proxy authorization matrix (the leak-closed contract)
# --------------------------------------------------------------------------- #
class TestProxyAuthorization:
    def test_no_token_is_401(self, client, stub_upstream):
        dataplane_auth.set_validator(_FakeValidator(False))
        resp = client.get("/skdashboard/api/board")
        assert resp.status_code == 401

    def test_valid_authorization_header_200_path(self, client, stub_upstream):
        dataplane_auth.set_validator(_FakeValidator(True))
        resp = client.get(
            "/skdashboard/api/board",
            headers={"Authorization": "Bearer valid"},
        )
        assert resp.status_code == 200
        assert resp.text == "OK"

    def test_valid_embed_token_200_path(self, client, stub_upstream):
        dataplane_auth.set_validator(_FakeValidator(False))  # no header credential
        token, _ = embed_auth.mint_embed_token("skdashboard")
        resp = client.get(f"/skdashboard/app?embed_token={token}")
        assert resp.status_code == 200
        assert resp.text == "OK"
        # First navigation issues a path-scoped cookie so subresource loads stay authed.
        assert embed_auth.cookie_name("skdashboard") in resp.cookies

    def test_embed_cookie_authorizes_subresource(self, client, stub_upstream):
        """A pane subresource (no query param) authorizes via the scoped cookie."""
        dataplane_auth.set_validator(_FakeValidator(False))
        token, _ = embed_auth.mint_embed_token("skdashboard")
        client.cookies.set(embed_auth.cookie_name("skdashboard"), token)
        resp = client.get("/skdashboard/api/board")  # no embed_token query param
        assert resp.status_code == 200

    def test_expired_embed_token_is_401(self, client, stub_upstream, monkeypatch):
        dataplane_auth.set_validator(_FakeValidator(False))
        # Backdate the mint clock so the token is already expired at verify time.
        real = time.time
        monkeypatch.setattr(time, "time", lambda: real() - 100)
        token, _ = embed_auth.mint_embed_token("skdashboard", ttl=1)
        monkeypatch.setattr(time, "time", real)
        resp = client.get(f"/skdashboard/app?embed_token={token}")
        assert resp.status_code == 401

    def test_foreign_scope_embed_token_is_401(self, client, stub_upstream):
        """A token minted for skos must NOT authorize the skdashboard proxy."""
        dataplane_auth.set_validator(_FakeValidator(False))
        token, _ = embed_auth.mint_embed_token("skos")
        resp = client.get(f"/skdashboard/api/board?embed_token={token}")
        assert resp.status_code == 401

    def test_embed_token_is_read_only(self, client, stub_upstream):
        """A POST (write) that only presents an embed token is refused (403)."""
        dataplane_auth.set_validator(_FakeValidator(False))
        token, _ = embed_auth.mint_embed_token("skdashboard")
        resp = client.post(
            f"/skdashboard/api/card/x/queue?embed_token={token}",
            json={},
        )
        assert resp.status_code == 403

    def test_full_auth_may_write(self, client, stub_upstream):
        """A full operator credential keeps read + write (POST) access."""
        dataplane_auth.set_validator(_FakeValidator(True))
        resp = client.post(
            "/skdashboard/api/card/x/queue",
            headers={"Authorization": "Bearer valid"},
            json={},
        )
        assert resp.status_code == 200

    def test_skos_proxy_embed_token(self, client, stub_upstream):
        dataplane_auth.set_validator(_FakeValidator(False))
        token, _ = embed_auth.mint_embed_token("skos")
        resp = client.get(f"/skos/app?embed_token={token}")
        assert resp.status_code == 200
