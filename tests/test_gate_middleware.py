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
