"""FastAPI routes for SK Spaces (S1: create/join/guest-join/list/end).

No SFU call at create time — LiveKit auto-creates the room when the host first
connects — so these routes are fully testable with a dummy key/secret.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from skchat.spaces.registry import SpaceRegistry
from skchat.spaces.roles import Role
from skchat.spaces.space import Space, derive_space_id
from skchat.spaces.tokens import mint_space_token

logger = logging.getLogger("skchat.spaces.routes")

_DEFAULT_TTL = int(os.getenv("SKCHAT_LIVEKIT_TOKEN_TTL", "21600"))


def _url() -> str:
    return os.getenv("SKCHAT_LIVEKIT_URL", "ws://skworld-100:7880")


def _have_creds() -> bool:
    return bool(os.getenv("SKCHAT_LIVEKIT_API_KEY") and
                os.getenv("SKCHAT_LIVEKIT_API_SECRET"))


def register_spaces_routes(app: FastAPI, *, registry: SpaceRegistry | None = None) -> None:
    reg = registry or SpaceRegistry()

    def _token_response(identity: str, name: str, role: Role, space: Space) -> dict:
        token = mint_space_token(identity, name, role, space.space_id, _DEFAULT_TTL)
        return {
            "space_id": space.space_id, "room": space.room, "url": _url(),
            "identity": identity, "name": name, "role": role.value, "token": token,
            "title": space.title,
        }

    @app.post("/spaces/create")
    async def create_space(request: Request) -> JSONResponse:
        if not _have_creds():
            raise HTTPException(503, "livekit not configured")
        body = await request.json()
        host = (body.get("host_fqid") or "").strip()
        title = (body.get("title") or "").strip()
        slug = (body.get("slug") or "").strip()
        if not (host and title and slug):
            raise HTTPException(400, "host_fqid, title, slug required")
        sid = derive_space_id(host, slug)
        space = Space(space_id=sid, host_fqid=host, title=title, slug=slug,
                      created_at=time.time())
        reg.add(space)
        return JSONResponse(_token_response(host, host.split("@")[0], Role.HOST, space))

    @app.post("/spaces/{space_id}/join")
    async def join_space(space_id: str, request: Request) -> JSONResponse:
        space = reg.get(space_id)
        if space is None or space.status.value == "ended":
            raise HTTPException(404, "space not found or ended")
        body = await request.json()
        identity = (body.get("identity") or "").strip()
        if not identity:
            raise HTTPException(400, "identity required")
        name = body.get("name") or identity.split("@")[0]
        return JSONResponse(_token_response(identity, name, Role.LISTENER, space))

    @app.post("/spaces/{space_id}/join-guest")
    async def join_space_guest(space_id: str, request: Request) -> JSONResponse:
        """Guest-link listener: verify a guest.py invite, then mint a LISTENER token."""
        space = reg.get(space_id)
        if space is None or space.status.value == "ended":
            raise HTTPException(404, "space not found or ended")
        body = await request.json()
        invite = (body.get("invite_token") or "").strip()
        display = (body.get("display") or "Guest").strip()
        if not invite:
            raise HTTPException(400, "invite_token required")
        from skchat.guest import GuestJoinError, InviteVerifier
        try:
            guest = InviteVerifier().verify(invite, expected_room=space_id,
                                            display_name=display)
        except GuestJoinError as exc:
            raise HTTPException(403, f"invalid invite: {exc}") from exc
        return JSONResponse(_token_response(guest.identity, guest.display or display,
                                            Role.LISTENER, space))

    @app.get("/spaces")
    async def list_spaces() -> JSONResponse:
        return JSONResponse({"spaces": [
            {"space_id": s.space_id, "title": s.title, "host_fqid": s.host_fqid,
             "status": s.status.value, "speakers": s.speakers}
            for s in reg.live()
        ]})

    @app.post("/spaces/{space_id}/end")
    async def end_space(space_id: str) -> JSONResponse:
        if reg.get(space_id) is None:
            raise HTTPException(404, "space not found")
        reg.end(space_id)
        return JSONResponse({"ok": True, "space_id": space_id})

    @app.get("/space/{space_id}", response_class=HTMLResponse)
    async def space_page(space_id: str) -> HTMLResponse:  # noqa: ARG001
        static = Path(__file__).resolve().parent.parent / "static" / "space.html"
        if static.exists():
            return FileResponse(static, media_type="text/html")
        return HTMLResponse("space.html missing", status_code=500)
