"""Call orchestration routes: start (ring), answer (no ring), incoming, ICE.

Builds on the deterministic room (call_session) + LiveKit token mint
(livekit_routes) + skcomms mailbox for the CALL_INVITE ring.
"""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .call_session import (
    CALL_INVITE_SUBJECT,
    build_invite_body,
    derive_room,
    parse_invite_body,
)
from .connectivity import ice_config
from .livekit_routes import LIVEKIT_URL, _have_creds, _mint_token

logger = logging.getLogger("skchat.call_routes")
_TOKEN_TTL = 21600  # 6 hours; tokens are non-revocable, keep short relative to key rotation

_CALL_RESPONSE_KEYS = ("room", "token", "livekit_url", "peer_fqid", "identity")


def _call_response(ctx: dict) -> JSONResponse:
    return JSONResponse({k: ctx[k] for k in _CALL_RESPONSE_KEYS})


# --- thin wrappers (monkeypatchable seams; keep I/O out of route bodies) -----
def _list_peers() -> dict:
    from skcomms.peers import list_peers

    return list_peers()


def _self_fqid() -> str:
    from capauth import resolve_agent_identity

    return resolve_agent_identity().fqid


def _send_invite(*, from_fqid: str, to_fqid: str, room: str, livekit_url: str) -> None:
    from skcomms.mailbox import send_message

    body = build_invite_body(
        from_fqid=from_fqid, to_fqid=to_fqid, room=room, livekit_url=livekit_url
    )
    send_message(to_fqid, body, subject=CALL_INVITE_SUBJECT)


def _read_inbox() -> list:
    from skcomms.mailbox import read_inbox

    return read_inbox()


def _resolve_peer(peer: str) -> str:
    """Resolve a peer arg (FQID or bare name) to a paired FQID, or 404."""
    peers = _list_peers()
    if peer in peers:
        return peer
    matches = [fqid for fqid in peers if fqid.split("@", 1)[0] == peer]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail=f"ambiguous bare name {peer!r}: matches {matches}; use full FQID",
        )
    raise HTTPException(status_code=404, detail=f"peer not paired: {peer}")


def _prepare_call(peer: str) -> dict:
    peer_fqid = _resolve_peer(peer)          # 404 if not paired (resolve first)
    if not _have_creds():                    # 503 only once the peer is valid
        raise HTTPException(status_code=503, detail="livekit not configured")
    local_fqid = _self_fqid()
    room = derive_room(local_fqid, peer_fqid)
    token = _mint_token(local_fqid, local_fqid.split("@", 1)[0], room, _TOKEN_TTL)
    return {
        "room": room,
        "token": token,
        "livekit_url": LIVEKIT_URL,
        "peer_fqid": peer_fqid,
        "identity": local_fqid,
    }


async def _peer_arg(request: Request) -> str:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = dict(await request.form())
    peer = (body.get("peer") or "").strip()
    if not peer:
        raise HTTPException(status_code=400, detail="peer required")
    return peer


def register_call_routes(app: FastAPI) -> None:
    @app.post("/call/start")
    async def call_start(request: Request) -> JSONResponse:
        peer = await _peer_arg(request)
        ctx = _prepare_call(peer)
        _send_invite(
            from_fqid=ctx["identity"],
            to_fqid=ctx["peer_fqid"],
            room=ctx["room"],
            livekit_url=ctx["livekit_url"],
        )
        return _call_response(ctx)

    @app.post("/call/answer")
    async def call_answer(request: Request) -> JSONResponse:
        peer = await _peer_arg(request)
        ctx = _prepare_call(peer)  # no _send_invite — answering never rings
        return _call_response(ctx)

    @app.get("/call/incoming")
    async def call_incoming() -> JSONResponse:
        """Surface CALL_INVITE envelopes addressed to us, newest first."""
        me = _self_fqid()
        invites = []
        for env, _verify in _read_inbox():
            if getattr(env, "subject", None) != CALL_INVITE_SUBJECT:
                continue
            if getattr(env, "to_fqid", None) != me:
                continue  # never trust an invite not addressed to us
            try:
                inv = parse_invite_body(env.body)
            except ValueError:
                continue
            invites.append(inv)
        invites.sort(key=lambda i: i.get("ts", 0), reverse=True)
        return JSONResponse({"invites": invites})

    @app.get("/connectivity/ice")
    async def connectivity_ice(peer: str) -> JSONResponse:
        peer_fqid = _resolve_peer(peer)
        local_fqid = _self_fqid()
        # Tailnet-first deployment: default to on_tailnet (tier 1). Off-tailnet
        # per-peer detection is not wired yet — add a hint param when it is.
        cfg = ice_config(local_fqid, peer_fqid, peer_hint={"on_tailnet": True})
        return JSONResponse(cfg)
