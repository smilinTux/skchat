"""Daemon API proxy — serves the endpoints the Flutter app needs by stitching
together skchat daemon, skcapstone API, and webui behind a single /api/* prefix."""

from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

logger = logging.getLogger("skchat.daemon_proxy")

router = APIRouter(prefix="/api")


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
