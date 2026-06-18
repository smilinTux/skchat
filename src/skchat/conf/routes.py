"""FastAPI routes for Sovereign Conf Calls (multi-party VIDEO conferences).

Mirrors ``skchat.spaces.routes`` in structure — host-gating via ``_require_host``,
the ``_have_creds`` dummy-cred-testable pattern, and the lazy ``LiveKitAPI`` /
``_service`` roster client borrowed from ``spaces.moderation.Moderator``.

A ``Conf`` is the video sibling of an audio ``Space``: everyone may publish
camera + mic + screenshare (up to ``participant_cap``). Token minting flows
through :func:`skchat.spaces.tokens.mint_conf_token`, which routes every grant
through the single ``conf_grant_for`` factory so a guest can never be
over-granted admin.

No SFU call happens at create/token time — LiveKit auto-creates the room when the
first participant connects — so create → token → end is fully testable with a
dummy key/secret. Only the live roster (``/conf/{room}/participants``) and the
optional room teardown on ``/conf/{room}/end`` touch the SFU, and both degrade
gracefully (empty / best-effort) when creds are absent or the SFU is unreachable.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from skchat.conf.room import Conf, ConfRegistry
from skchat.spaces.roles import ConfRole
from skchat.spaces.tokens import mint_conf_token

logger = logging.getLogger("skchat.conf.routes")

_DEFAULT_TTL = int(os.getenv("SKCHAT_LIVEKIT_TOKEN_TTL", "21600"))


def _url() -> str:
    return os.getenv("SKCHAT_LIVEKIT_URL", "ws://skworld-100:7880")


def _http_url(ws_url: str) -> str:
    return ws_url.replace("ws://", "http://").replace("wss://", "https://")


def _have_creds() -> bool:
    return bool(os.getenv("SKCHAT_LIVEKIT_API_KEY") and os.getenv("SKCHAT_LIVEKIT_API_SECRET"))


def register_conf_routes(
    app: FastAPI,
    *,
    registry: ConfRegistry | None = None,
    room_service=None,
) -> None:
    """Wire the conference REST API onto ``app``.

    ``registry`` and ``room_service`` are injectable for tests; in production a
    default ``ConfRegistry`` (``~/.skchat/confs.json``) is used and the LiveKit
    ``RoomService`` is built lazily from the env creds on first roster/teardown.
    """
    reg = registry or ConfRegistry()
    # Lazy LiveKit room-service client (mirrors Moderator._service): only built
    # when a route actually needs the live SFU, and only if creds are present.
    _svc_holder = {"svc": room_service}

    def _service():
        """Return a LiveKit RoomService, or None if creds are missing / livekit
        is not installed. Never raises — callers degrade gracefully on None."""
        if _svc_holder["svc"] is not None:
            return _svc_holder["svc"]
        if not _have_creds():
            return None
        try:
            from livekit import api

            client = api.LiveKitAPI(
                _http_url(_url()),
                os.getenv("SKCHAT_LIVEKIT_API_KEY", ""),
                os.getenv("SKCHAT_LIVEKIT_API_SECRET", ""),
            )
            _svc_holder["svc"] = client.room
        except Exception as exc:  # noqa: BLE001 - missing livekit / bad creds → degrade
            logger.warning("conf: could not build LiveKit room service: %s", exc)
            return None
        return _svc_holder["svc"]

    def _require_host(conf: Conf, requester: str) -> None:
        if not conf.host_fqid.strip() or requester != conf.host_fqid:
            raise HTTPException(403, "host-only action")

    def _join_url(room: str) -> str:
        return f"/conf/{room}"

    @app.post("/conf/create")
    async def create_conf(request: Request) -> JSONResponse:
        # SECURITY: like /spaces/create, this trusts the tailnet — host_fqid is
        # asserted, not proven, so a SOVEREIGN (optionally room_admin) token is
        # minted for whoever asks. Tailnet-only until the identity epic verifies a
        # capauth-signed operator assertion through the /conf/{room}/token seam.
        # Do NOT expose this route publicly before that hardening lands.
        if not _have_creds():
            raise HTTPException(503, "livekit not configured")
        body = await request.json()
        host = (body.get("host_fqid") or "").strip()
        title = (body.get("title") or "").strip()
        # slug is optional: omitted → ad-hoc "new meeting" room (random suffix);
        # given → deterministic, re-derivable named room.
        raw_slug = body.get("slug")
        slug = raw_slug.strip() if isinstance(raw_slug, str) else None
        if not (host and title):
            raise HTTPException(400, "host_fqid and title required")
        # C1 defense-in-depth: cap title length to blunt a stored-XSS payload.
        if len(title) > 120:
            raise HTTPException(400, "title too long (max 120 chars)")
        conf = reg.create(host, title, slug=slug or None)
        # Host is SOVEREIGN with room_admin so it can moderate / tear down.
        token = mint_conf_token(
            host,
            host.split("@")[0],
            ConfRole.SOVEREIGN,
            conf.room,
            _DEFAULT_TTL,
            sovereign_admin=True,
        )
        return JSONResponse(
            {
                "conf_id": conf.conf_id,
                "room": conf.room,
                "url": _url(),
                "identity": host,
                "name": host.split("@")[0],
                "role": ConfRole.SOVEREIGN.value,
                "token": token,
                "title": conf.title,
                "join_url": _join_url(conf.room),
            }
        )

    @app.post("/conf/{room}/token")
    async def conf_token(room: str, request: Request) -> JSONResponse:
        """Mint a role-scoped conference token for ``room`` (default PARTICIPANT).

        This is the SINGLE join contract: the identity epic plugs sovereign-vs-
        guest decisioning in here (validate a signed assertion → choose role /
        sovereign_admin) without touching create/end. Keep the seam clean —
        every conf join, human or agent, flows through this one route.
        """
        conf = reg.get(room)
        if conf is None or conf.status.value == "ended":
            raise HTTPException(404, "conf not found or ended")
        body = await request.json()
        identity = (body.get("identity") or "").strip()
        if not identity:
            raise HTTPException(400, "identity required")
        name = (body.get("name") or "").strip() or identity.split("@")[0]
        role_raw = (body.get("role") or ConfRole.PARTICIPANT.value).strip()
        try:
            role = ConfRole(role_raw)
        except ValueError as exc:
            raise HTTPException(400, f"unknown conf role: {role_raw!r}") from exc
        token = mint_conf_token(identity, name, role, conf.room, _DEFAULT_TTL)
        return JSONResponse(
            {
                "conf_id": conf.conf_id,
                "room": conf.room,
                "url": _url(),
                "identity": identity,
                "name": name,
                "role": role.value,
                "token": token,
                "title": conf.title,
            }
        )

    @app.get("/conf/{room}/participants")
    async def conf_participants(room: str) -> JSONResponse:
        """LIVE roster from the SFU. Degrades gracefully: no creds / no livekit /
        unreachable SFU → an empty list + ``live=false`` rather than a 5xx."""
        conf = reg.get(room)
        if conf is None:
            raise HTTPException(404, "conf not found")
        svc = _service()
        if svc is None:
            return JSONResponse({"room": room, "participants": [], "live": False})
        try:
            from livekit import api

            resp = await svc.list_participants(api.ListParticipantsRequest(room=conf.room))
            participants = [
                {
                    "identity": p.identity,
                    "name": getattr(p, "name", "") or "",
                    "state": getattr(
                        getattr(p, "state", None), "name", str(getattr(p, "state", ""))
                    ),
                    "joined_at": getattr(p, "joined_at", 0),
                }
                for p in resp.participants
            ]
        except Exception as exc:  # noqa: BLE001 - roster is best-effort, never 500
            logger.warning("conf: list_participants failed for %s: %s", conf.room, exc)
            return JSONResponse({"room": room, "participants": [], "live": False})
        return JSONResponse({"room": room, "participants": participants, "live": True})

    @app.post("/conf/{room}/end")
    async def end_conf(room: str, request: Request) -> JSONResponse:
        """Mark the Conf ended (host-gated) and best-effort delete the SFU room."""
        conf = reg.get(room)
        if conf is None:
            raise HTTPException(404, "conf not found")
        body = await request.json()
        _require_host(conf, (body.get("requester") or "").strip())
        reg.end(room)
        room_deleted = False
        svc = _service()
        if svc is not None:
            try:
                from livekit import api

                await svc.delete_room(api.DeleteRoomRequest(room=conf.room))
                room_deleted = True
            except Exception as exc:  # noqa: BLE001 - teardown is best-effort
                logger.warning("conf: delete_room failed for %s: %s", conf.room, exc)
        return JSONResponse({"ok": True, "conf_id": conf.conf_id, "room_deleted": room_deleted})

    @app.get("/conf")
    async def list_confs() -> JSONResponse:
        return JSONResponse(
            {
                "confs": [
                    {
                        "conf_id": c.conf_id,
                        "title": c.title,
                        "host_fqid": c.host_fqid,
                        "status": c.status.value,
                        "participants": c.participants,
                        "participant_cap": c.participant_cap,
                        "recording": c.recording,
                    }
                    for c in reg.list_live()
                ]
            }
        )

    @app.get("/conf/{room}", response_class=HTMLResponse)
    async def conf_page(room: str) -> HTMLResponse:  # noqa: ARG001
        """Serve the conference web client (mirrors livekit_routes' /livekit/{room}).

        The page reads the conf room from its own URL path, mints a join token via
        ``POST /conf/{room}/token``, and joins the SFU — so a bare ``/conf/<room>``
        link is all a participant needs. ``room`` is unused server-side (the static
        HTML does the routing) but is part of the URL contract.
        """
        static = Path(__file__).resolve().parent.parent / "static" / "conf.html"
        if static.exists():
            return FileResponse(static, media_type="text/html")
        return HTMLResponse("conf.html missing", status_code=500)
