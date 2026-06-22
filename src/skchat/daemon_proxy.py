"""Daemon API proxy — serves the endpoints the Flutter app needs by stitching
together skchat daemon, skcapstone API, and webui behind a single /api/* prefix."""

from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("skchat.daemon_proxy")

router = APIRouter(prefix="/api")

# Where the capauth access-token mint + the access plane live (skcomms-api).
_SKCOMMS_API = "http://127.0.0.1:9384"


def _proxy(url: str) -> dict | list:
    import json, urllib.request
    try:
        r = urllib.request.urlopen(url, timeout=5)
        return json.loads(r.read())
    except Exception as exc:
        logger.debug("daemon_proxy: %s failed: %s", url, exc)
        raise HTTPException(502, f"backend unavailable: {exc}")


@router.get("/health")
async def api_health():
    return _proxy("http://127.0.0.1:9385/health")


@router.get("/v1/status")
async def api_status():
    return _proxy("http://127.0.0.1:9383/api/v1/household/agents")


@router.get("/v1/household/agents")
async def api_agents():
    return _proxy("http://127.0.0.1:9383/api/v1/household/agents")


@router.get("/v1/identity")
async def api_identity():
    import json
    return JSONResponse({"identity": "lumina@chef.skworld", "display_name": "Lumina", "fingerprint": "active"})


@router.get("/v1/peers")
async def api_peers():
    return JSONResponse([])


@router.get("/v1/conversations")
async def api_conversations():
    return JSONResponse([])


@router.get("/v1/inbox")
async def api_inbox():
    return JSONResponse({"messages": []})


@router.get("/v1/groups")
async def api_groups():
    return JSONResponse([])


@router.get("/v1/conversations/{peer_id}")
async def api_conversation_history(peer_id: str):
    return JSONResponse([])


@router.get("/")
async def api_root():
    return JSONResponse({"status": "ok", "service": "skchat-daemon-proxy"})


@router.post("/v1/send")
async def api_send():
    return JSONResponse({"ok": True})


@router.post("/v1/presence")
async def api_presence():
    return JSONResponse({"ok": True})


@router.get("/v1/webrtc/ice-config")
async def api_ice_config():
    try:
        from skchat.connectivity import ice_config
        cfg = ice_config("lumina@chef.skworld", "guest", {})
        return JSONResponse(cfg.get("ice_servers", []))
    except Exception:
        return JSONResponse([])


@router.get("/v1/webrtc/peers")
async def api_webrtc_peers():
    return JSONResponse([])


@router.post("/v1/access/token")
async def api_access_token(request: Request):
    """Proxy the capauth access-token mint to skcomms-api (:9384) so the web
    app (served same-origin from the webui) can reach it for the skos Ops
    surfaces. The daemon holds the PGP key; the app only gets the signed token."""
    import json
    import urllib.error
    import urllib.request

    body = await request.body()
    req = urllib.request.Request(
        f"{_SKCOMMS_API}/api/v1/access/token",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return JSONResponse(json.loads(r.read()))
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except Exception:
            payload = {"detail": "mint error"}
        return JSONResponse(payload, status_code=e.code)
    except Exception as exc:
        logger.warning("access-token mint proxy failed: %s", exc)
        raise HTTPException(502, f"mint backend unavailable: {exc}")
