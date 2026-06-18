"""Daemon API proxy — serves the endpoints the Flutter app needs by stitching
together the skchat daemon (health), skcapstone API, and webui API behind a
single base URL. Registered in webui.py as /api/* routes."""

from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

logger = logging.getLogger("skchat.daemon_proxy")

router = APIRouter(prefix="/api")


def _proxy(url: str) -> dict:
    import urllib.request, json
    try:
        r = urllib.request.urlopen(url, timeout=5)
        return json.loads(r.read())
    except Exception as exc:
        logger.debug("daemon_proxy: %s failed: %s", url, exc)
        raise HTTPException(502, f"backend unavailable: {exc}")


@router.get("/health")
async def api_health():
    """Health check — delegates to skchat daemon health server."""
    return _proxy("http://127.0.0.1:9385/health")


@router.get("/v1/status")
async def api_status():
    """Daemon status — delegates to skcapstone API."""
    return _proxy("http://127.0.0.1:9383/api/v1/household/agents")


@router.get("/v1/conversations/{peer_id}")
async def api_conversations(peer_id: str):
    """Conversation history — delegates to skchat daemon."""
    try:
        from skchat.history import ChatHistory
        hist = ChatHistory()
        msgs = hist.load(peer=peer_id, limit=50)
        return JSONResponse([m.to_dict() if hasattr(m, 'to_dict') else {"sender": m.sender, "text": m.text} for m in msgs])
    except Exception as exc:
        raise HTTPException(502, f"history unavailable: {exc}")


@router.get("/v1/household/agents")
async def api_agents():
    """Agent list — delegates to skcapstone API."""
    return _proxy("http://127.0.0.1:9383/api/v1/household/agents")
