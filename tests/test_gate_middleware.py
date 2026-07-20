from skchat.dataplane_paths import is_gated


def test_sensitive_paths_are_gated():
    assert is_gated("GET", "/api/v1/conversations") is True
    assert is_gated("POST", "/api/v1/send") is True
    assert is_gated("GET", "/api/v1/peers") is True


def test_exempt_paths_are_open():
    assert is_gated("GET", "/health") is False
    assert is_gated("GET", "/api/health") is False
    assert is_gated("POST", "/api/v1/inbox") is False      # federation S2S
    assert is_gated("GET", "/api/v1/auth/challenge") is False  # bootstrap
    assert is_gated("POST", "/api/v1/auth/session") is False   # bootstrap
    assert is_gated("GET", "/app/index.html") is False
    assert is_gated("GET", "/.well-known/skfed/directory") is False


def test_method_specific_inbox():
    assert is_gated("GET", "/api/v1/inbox") is True    # reading YOUR inbox
    assert is_gated("POST", "/api/v1/inbox") is False  # peers delivering to you


def test_guest_and_mode_c_routes_are_exempt():
    # Guest flows carry their own auth (guest-session JWT / invite token);
    # the operator-session validator does not accept those credentials, so
    # these families must stay exempt or every guest flow would 401.
    assert is_gated("POST", "/api/v1/guest/join") is False
    assert is_gated("GET", "/api/v1/guest/conversation") is False
    assert is_gated("POST", "/api/v1/mode-c/accept") is False
    # Regression: the core unguarded data routes stay gated.
    assert is_gated("GET", "/api/v1/conversations") is True


def test_exempt_prefix_anchoring():
    # Anchor exempt-prefix matches to path-segment boundaries to prevent
    # silent-leak traps where future routes sharing a prefix get wrongly exempted.
    # Examples: /api/v1/guest/ is exempt, but /api/v1/guestbook/ must stay gated.
    assert is_gated("GET", "/api/v1/guestbook") is True
    assert is_gated("GET", "/api/v1/mode-config") is True
    # Real exemptions still work.
    assert is_gated("POST", "/api/v1/guest/join") is False
    assert is_gated("POST", "/api/v1/mode-c/accept") is False


def test_prekey_directory_is_method_aware():
    # Public PQ key directory: peers fetch bundles unauthenticated (GET), but
    # publishing a bundle (POST) is an operator-owned write and stays gated.
    assert is_gated("GET", "/api/v1/prekey/lumina") is False
    assert is_gated("GET", "/api/v1/prekey") is False
    assert is_gated("POST", "/api/v1/prekey") is True


def test_file_bytes_are_gated():
    # Raw attachment bytes are the same data class as the gated /inbox.
    assert is_gated("GET", "/file/abc123") is True
    assert is_gated("GET", "/file/abc123/thumb") is True


def test_media_file_stream_is_gated():
    assert is_gated("GET", "/media/file") is True


def test_coord_board_proxy_is_gated():
    assert is_gated("GET", "/api/board") is True


def test_adapters_health_is_gated():
    assert is_gated("GET", "/adapters") is True


def test_identity_and_capabilities_bootstrap_are_exempt():
    # Pre-session UI bootstrap reads: low-sensitivity discovery surfaces the
    # app fetches before a session exists. Gating them would break an
    # unenrolled/unauthed client's startup.
    assert is_gated("GET", "/api/v1/identity") is False
    assert is_gated("GET", "/api/v1/capabilities") is False
    # Regression: adjacent, more sensitive /api/v1 routes stay gated.
    assert is_gated("GET", "/api/v1/status") is True
    assert is_gated("GET", "/api/v1/household/agents") is True


def test_favicon_ico_is_exempt():
    assert is_gated("GET", "/favicon.ico") is False


from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from skchat import operator_auth as oa
from skchat.dataplane_auth import dataplane_auth_enabled, enforce_dataplane_auth


def _build_app():
    app = FastAPI()

    @app.get("/api/v1/conversations")
    async def convos():
        return [{"peer_id": "x"}]

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.middleware("http")
    async def gate(request, call_next):
        if dataplane_auth_enabled() and is_gated(request.method, request.url.path):
            try:
                enforce_dataplane_auth(request)
            except Exception:
                return JSONResponse({"detail": "capauth authentication required"}, 401)
        return await call_next(request)

    return app


def test_flag_off_passthrough(monkeypatch):
    monkeypatch.delenv("SKCHAT_DATAPLANE_AUTH", raising=False)
    c = TestClient(_build_app())
    assert c.get("/api/v1/conversations").status_code == 200


def test_flag_on_blocks_unauthed(monkeypatch):
    monkeypatch.setenv("SKCHAT_DATAPLANE_AUTH", "1")
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "sec")
    c = TestClient(_build_app())
    assert c.get("/api/v1/conversations").status_code == 401
    assert c.get("/health").status_code == 200


def test_flag_on_allows_valid_session(monkeypatch):
    monkeypatch.setenv("SKCHAT_DATAPLANE_AUTH", "1")
    monkeypatch.setenv("SKCHAT_OPERATOR_TOKEN_SECRET", "sec")
    tok = oa.mint_operator_session(device_fp="abc", ttl=60)
    c = TestClient(_build_app())
    assert (
        c.get(
            "/api/v1/conversations", headers={"Authorization": f"Bearer {tok}"}
        ).status_code
        == 200
    )
