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


def register_spaces_routes(app: FastAPI, *, registry: SpaceRegistry | None = None,
                           moderator=None, consent=None, recorder=None) -> None:
    reg = registry or SpaceRegistry()
    _mod_holder = {"mod": moderator}
    from skchat.spaces.consent import ConsentLedger
    led = consent or ConsentLedger()
    _rec_holder = {"rec": recorder}

    def _moderator():
        if _mod_holder["mod"] is None:
            from skchat.spaces.moderation import Moderator
            _mod_holder["mod"] = Moderator(
                _url(), os.getenv("SKCHAT_LIVEKIT_API_KEY", ""),
                os.getenv("SKCHAT_LIVEKIT_API_SECRET", ""))
        return _mod_holder["mod"]

    def _recorder():
        if _rec_holder["rec"] is None:
            from skchat.spaces.recording import Recorder
            _rec_holder["rec"] = Recorder(
                _url(), os.getenv("SKCHAT_LIVEKIT_API_KEY", ""),
                os.getenv("SKCHAT_LIVEKIT_API_SECRET", ""))
        return _rec_holder["rec"]

    def _require_host(space, requester: str) -> None:
        if requester != space.host_fqid:
            raise HTTPException(403, "host-only action")

    def _token_response(identity: str, name: str, role: Role, space: Space) -> dict:
        token = mint_space_token(identity, name, role, space.space_id, _DEFAULT_TTL)
        return {
            "space_id": space.space_id, "room": space.room, "url": _url(),
            "identity": identity, "name": name, "role": role.value, "token": token,
            "title": space.title,
        }

    @app.post("/spaces/create")
    async def create_space(request: Request) -> JSONResponse:
        # SECURITY: S1/S2 trust the tailnet — host_fqid is asserted, not proven, so
        # this endpoint mints a roomAdmin token for whoever asks. Tailnet-only until
        # S5 sk-lk-authd verifies a capauth-signed operator assertion. Do NOT expose
        # this route publicly before that hardening lands.
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
             "status": s.status.value, "speakers": s.speakers,
             "recording": s.recording}
            for s in reg.live()
        ]})

    @app.post("/spaces/{space_id}/end")
    async def end_space(space_id: str, request: Request) -> JSONResponse:
        space = reg.get(space_id)
        if space is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        _require_host(space, (body.get("requester") or "").strip())
        reg.end(space_id)
        return JSONResponse({"ok": True, "space_id": space_id})

    @app.post("/spaces/{space_id}/raise-hand")
    async def raise_hand(space_id: str, request: Request) -> JSONResponse:
        space = reg.get(space_id)
        if space is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        identity = (body.get("identity") or "").strip()
        if not identity:
            raise HTTPException(400, "identity required")
        on_stage = await _moderator().stage_action(space.room, identity, "raise_hand")
        return JSONResponse({"ok": True, "on_stage": on_stage})

    @app.post("/spaces/{space_id}/invite")
    async def invite(space_id: str, request: Request) -> JSONResponse:
        space = reg.get(space_id)
        if space is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        _require_host(space, (body.get("requester") or "").strip())
        identity = (body.get("identity") or "").strip()
        if not identity:
            raise HTTPException(400, "identity required")
        on_stage = await _moderator().stage_action(space.room, identity, "invite")
        return JSONResponse({"ok": True, "on_stage": on_stage})

    @app.post("/spaces/{space_id}/remove-from-stage")
    async def remove_from_stage(space_id: str, request: Request) -> JSONResponse:
        space = reg.get(space_id)
        if space is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        requester = (body.get("requester") or "").strip()
        identity = (body.get("identity") or "").strip()
        # host OR self may remove from stage
        if requester != space.host_fqid and requester != identity:
            raise HTTPException(403, "host-or-self only")
        await _moderator().stage_action(space.room, identity, "remove")
        return JSONResponse({"ok": True})

    @app.post("/spaces/{space_id}/mute")
    async def mute(space_id: str, request: Request) -> JSONResponse:
        space = reg.get(space_id)
        if space is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        _require_host(space, (body.get("requester") or "").strip())
        await _moderator().mute(space.room, (body.get("identity") or "").strip(),
                                (body.get("track_sid") or "").strip())
        return JSONResponse({"ok": True})

    @app.post("/spaces/{space_id}/kick")
    async def kick(space_id: str, request: Request) -> JSONResponse:
        space = reg.get(space_id)
        if space is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        _require_host(space, (body.get("requester") or "").strip())
        await _moderator().kick(space.room, (body.get("identity") or "").strip())
        return JSONResponse({"ok": True})

    @app.post("/spaces/{space_id}/consent")
    async def record_consent(space_id: str, request: Request) -> JSONResponse:
        if reg.get(space_id) is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        identity = (body.get("identity") or "").strip()
        if not identity:
            raise HTTPException(400, "identity required")
        led.add(space_id, identity)
        return JSONResponse({"ok": True})

    @app.post("/spaces/{space_id}/record/start")
    async def record_start(space_id: str, request: Request) -> JSONResponse:
        from pathlib import Path as _P

        from skchat.spaces.consent import can_record
        space = reg.get(space_id)
        if space is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        _require_host(space, (body.get("requester") or "").strip())
        speakers = body.get("speakers") or []
        ok, missing = can_record(speakers, space_id, led)
        if not ok:
            return JSONResponse({"ok": False, "missing_consent": missing},
                                status_code=409)
        rec_dir = _P.home() / ".skchat" / "spaces-recordings"
        rec_dir.mkdir(parents=True, exist_ok=True)
        filepath = str(rec_dir / f"{space_id}.ogg")
        egress_id = await _recorder().start(space.room, filepath)
        reg.set_recording(space_id, True, egress_id)
        return JSONResponse({"ok": True, "egress_id": egress_id, "path": filepath})

    @app.post("/spaces/{space_id}/record/stop")
    async def record_stop(space_id: str, request: Request) -> JSONResponse:
        space = reg.get(space_id)
        if space is None:
            raise HTTPException(404, "space not found")
        body = await request.json()
        _require_host(space, (body.get("requester") or "").strip())
        if space.egress_id:
            await _recorder().stop(space.egress_id)
        reg.set_recording(space_id, False, "")
        return JSONResponse({"ok": True})

    @app.get("/spaces/live", response_class=HTMLResponse)
    async def spaces_directory() -> HTMLResponse:
        static = Path(__file__).resolve().parent.parent / "static" / "spaces.html"
        if static.exists():
            return FileResponse(static, media_type="text/html")
        return HTMLResponse("spaces.html missing", status_code=500)

    @app.get("/space/{space_id}", response_class=HTMLResponse)
    async def space_page(space_id: str) -> HTMLResponse:  # noqa: ARG001
        static = Path(__file__).resolve().parent.parent / "static" / "space.html"
        if static.exists():
            return FileResponse(static, media_type="text/html")
        return HTMLResponse("space.html missing", status_code=500)
