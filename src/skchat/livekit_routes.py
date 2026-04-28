"""LiveKit routes for skchat — token mint + room helper for video sessions.

skchat does NOT host an SFU. It mints short-lived JWTs that browser/agent
clients use to connect directly to a livekit-server (over the tailnet).

Endpoints:
    GET  /livekit/config        — public client config (URL, identity hint)
    POST /livekit/token         — mint a JWT for {room, identity, name}
    GET  /livekit               — browser video page (Lumina + peers)
    GET  /livekit/{room}        — browser video page joined to a specific room

Required env (with sane defaults for local-tailnet single-host setups):
    SKCHAT_LIVEKIT_URL         ws://skworld-100:7880
    SKCHAT_LIVEKIT_API_KEY     dev-key
    SKCHAT_LIVEKIT_API_SECRET  dev-secret-change-me
    SKCHAT_LIVEKIT_DEFAULT_ROOM   lumina-and-chef

If livekit-api isn't installed the routes return 503 with a clear hint —
the rest of skchat keeps working.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

logger = logging.getLogger("skchat.livekit_routes")

LIVEKIT_URL = os.getenv("SKCHAT_LIVEKIT_URL", "ws://skworld-100:7880")
LIVEKIT_API_KEY = os.getenv("SKCHAT_LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("SKCHAT_LIVEKIT_API_SECRET", "")
DEFAULT_ROOM = os.getenv("SKCHAT_LIVEKIT_DEFAULT_ROOM", "lumina-and-chef")
DEFAULT_TTL_SECONDS = int(os.getenv("SKCHAT_LIVEKIT_TOKEN_TTL", "21600"))  # 6h


def _have_creds() -> bool:
    return bool(LIVEKIT_API_KEY and LIVEKIT_API_SECRET)


def _mint_token(identity: str, name: str, room: str, ttl: int) -> str:
    """Build a participant JWT. Raises if livekit-api isn't installed."""
    from datetime import timedelta

    from livekit import api  # local import — soft dep

    grant = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
    )
    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(name)
        .with_grants(grant)
        .with_ttl(timedelta(seconds=ttl))
    )
    return token.to_jwt()


def register_livekit_routes(app: FastAPI) -> None:
    """Register LiveKit endpoints on the FastAPI app."""

    @app.get("/livekit/config")
    async def livekit_config() -> JSONResponse:
        """Browser-safe config; never includes the API secret."""
        return JSONResponse(
            {
                "url": LIVEKIT_URL,
                "default_room": DEFAULT_ROOM,
                "token_endpoint": "/livekit/token",
                "available": _have_creds(),
            }
        )

    @app.post("/livekit/token")
    async def livekit_token(request: Request) -> JSONResponse:
        """Mint a participant JWT.

        Body (JSON or form):
            identity: stable participant id (e.g. "chef" or "lumina")
            name: display name
            room: room name (defaults to env DEFAULT_ROOM)
            ttl: seconds (clamped to [60, 86400])
        """
        if not _have_creds():
            raise HTTPException(
                status_code=503,
                detail="livekit not configured: set SKCHAT_LIVEKIT_API_KEY/SECRET",
            )

        try:
            body = await request.json()
        except Exception:
            body = {}
        if not body:
            form = await request.form()
            body = dict(form)

        identity = (body.get("identity") or "").strip()
        if not identity:
            raise HTTPException(status_code=400, detail="identity required")

        name = body.get("name") or identity
        room = body.get("room") or DEFAULT_ROOM
        try:
            ttl = max(60, min(86400, int(body.get("ttl") or DEFAULT_TTL_SECONDS)))
        except (TypeError, ValueError):
            ttl = DEFAULT_TTL_SECONDS

        try:
            token = _mint_token(identity, name, room, ttl)
        except ImportError:
            raise HTTPException(
                status_code=503,
                detail="livekit-api not installed: pip install livekit-api",
            )
        except Exception as exc:
            logger.exception("token mint failed")
            raise HTTPException(status_code=500, detail=f"mint failed: {exc}") from exc

        return JSONResponse(
            {
                "url": LIVEKIT_URL,
                "room": room,
                "identity": identity,
                "name": name,
                "token": token,
                "ttl_seconds": ttl,
            }
        )

    @app.post("/livekit/speak")
    async def livekit_speak(request: Request) -> JSONResponse:
        """Push a JSON data packet into a room so the Lumina agent can speak it.

        Body:
            text: words to synthesize (required)
            room: target room (defaults to env DEFAULT_ROOM)
            destination: identity to direct the message to (default: ``lumina``).
                The agent simply listens for ``action=speak`` packets, so any
                identity that's running lumina-call.py will pick it up.
        """
        if not _have_creds():
            raise HTTPException(status_code=503, detail="livekit not configured")

        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else dict(await request.form())
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text required")

        room_name = body.get("room") or DEFAULT_ROOM
        destination = body.get("destination") or "lumina"

        try:
            from livekit import api  # type: ignore
        except ImportError as exc:
            raise HTTPException(status_code=503, detail="livekit-api not installed") from exc

        # SendData over the LiveKit room service (HTTP API on the SFU).
        http_url = LIVEKIT_URL.replace("ws://", "http://").replace("wss://", "https://")
        lk_api = api.LiveKitAPI(http_url, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        try:
            payload = json.dumps({"action": "speak", "text": text}).encode()
            req = api.SendDataRequest(
                room=room_name,
                data=payload,
                kind=api.DataPacket.Kind.RELIABLE,
                destination_identities=[destination],
                topic="lumina.control",
            )
            await lk_api.room.send_data(req)
        except Exception as exc:
            logger.exception("send_data failed")
            raise HTTPException(status_code=500, detail=f"send_data failed: {exc}") from exc
        finally:
            await lk_api.aclose()

        return JSONResponse({"ok": True, "room": room_name, "to": destination, "text": text})

    @app.get("/livekit", response_class=HTMLResponse)
    async def livekit_page() -> HTMLResponse:
        return _serve_livekit_html()

    @app.get("/livekit/{room}", response_class=HTMLResponse)
    async def livekit_room_page(room: str) -> HTMLResponse:  # noqa: ARG001
        return _serve_livekit_html()


def _serve_livekit_html() -> HTMLResponse:
    static = Path(__file__).parent / "static" / "livekit.html"
    if static.exists():
        return FileResponse(static, media_type="text/html")
    return HTMLResponse(
        "<h1>livekit.html missing</h1><p>Expected at "
        f"{static}. Run scripts/install-livekit-page.sh.</p>",
        status_code=500,
    )
