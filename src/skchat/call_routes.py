"""Call orchestration routes: start (ring), answer (no ring), incoming, ICE.

Builds on the deterministic room (call_session) + LiveKit token mint
(livekit_routes) + skcomms mailbox for the CALL_INVITE ring.
"""

from __future__ import annotations

import ipaddress
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


def _send_invite(
    *, from_fqid: str, to_fqid: str, room: str, livekit_url: str, topic: str = ""
) -> None:
    from skcomms.mailbox import send_message

    body = build_invite_body(
        from_fqid=from_fqid, to_fqid=to_fqid, room=room, livekit_url=livekit_url, topic=topic
    )
    send_message(to_fqid, body, subject=CALL_INVITE_SUBJECT)


def _alert_operator(*, from_fqid: str, to_fqid: str, room: str, topic: str = "") -> None:
    """Notify the operator (sk-alert + one-press join link). Never raises."""
    try:
        from .call_observability import alert_operator

        alert_operator(from_fqid=from_fqid, to_fqid=to_fqid, room=room, topic=topic)
    except Exception as exc:  # noqa: BLE001 — observability must not break the call
        logger.debug("operator alert skipped: %s", exc)


def _read_inbox() -> list:
    from skcomms.mailbox import read_inbox

    return read_inbox()


# Address ranges that count as "the caller can reach us directly, no relay".
# Loopback (127.0.0.0/8, ::1) is DELIBERATELY EXCLUDED: this host sits behind
# Tailscale Funnel (and can sit behind any reverse proxy), which terminates TLS
# locally and forwards the request to the app over loopback. So a genuine
# off-tailnet phone on cellular arrives as request.client.host == 127.0.0.1.
# Trusting bare loopback would misclassify that caller as on-tailnet and hand
# back an empty ice_servers list (Tier 1, no relay), and its media would never
# connect. We instead resolve the *real* client from forwarded headers and only
# treat genuine tailnet/LAN addresses as on-tailnet.
_ONTAILNET_NETS = (
    ipaddress.ip_network("100.64.0.0/10"),  # Tailscale CGNAT (tailnet IPv4)
    ipaddress.ip_network("10.0.0.0/8"),  # RFC1918 LAN
    ipaddress.ip_network("172.16.0.0/12"),  # RFC1918 LAN
    ipaddress.ip_network("192.168.0.0/16"),  # RFC1918 LAN
    ipaddress.ip_network("fc00::/7"),  # IPv6 ULA (incl. Tailscale fd7a:...)
)


def _parse_ip(raw: str | None) -> ipaddress._BaseAddress | None:
    """Best-effort parse of an IP string, stripping any brackets/port; None on failure."""
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("["):  # bracketed IPv6 literal, e.g. [::1]:8443
        end = raw.find("]")
        raw = raw[1:end] if end != -1 else raw[1:]
    elif raw.count(":") == 1:  # host:port (a bare IPv6 literal has >1 colon)
        raw = raw.split(":", 1)[0]
    try:
        return ipaddress.ip_address(raw)
    except ValueError:
        return None


def _real_client_ip(request: Request) -> ipaddress._BaseAddress | None:
    """Resolve the true client IP, honoring reverse-proxy / Tailscale Funnel headers.

    The direct socket peer (``request.client.host``) is not trustworthy on this
    host: Tailscale Funnel and any reverse proxy terminate the connection locally
    and forward to the app over loopback, so the peer is 127.0.0.1 even for a
    genuine off-tailnet caller. When the direct peer is loopback (a local proxy)
    or the request carries Funnel/forwarding signals, we honor the leftmost
    ``X-Forwarded-For`` entry (the original client) to recover the real address.
    A direct, non-loopback peer with no forwarding is trusted as-is.
    """
    headers = getattr(request, "headers", None) or {}
    client = getattr(request, "client", None)
    direct_ip = _parse_ip(getattr(client, "host", None) if client is not None else None)

    proxied = direct_ip is not None and direct_ip.is_loopback
    # Tailscale Funnel stamps every proxied request with this header; its presence
    # means the request came in over the public funnel ingress (no tailnet identity).
    funnel = bool(headers.get("tailscale-funnel-request"))
    xff = headers.get("x-forwarded-for") or ""

    if (proxied or funnel) and xff:
        forwarded = _parse_ip(xff.split(",")[0])
        if forwarded is not None:
            return forwarded
    # Loopback with no usable forwarded header cannot prove tailnet membership:
    # fall through with the loopback address so it is classified OFF-tailnet.
    return direct_ip


def _client_on_tailnet(request: Request) -> bool:
    """True ONLY for a genuine tailnet/LAN caller (direct host candidates suffice).

    This is the reachability that matters for /connectivity/ice: the browser
    making this request needs to know whether it can rely on direct host
    candidates, not the peer it is calling. Off-tailnet callers (mobile data,
    home wifi w/o Tailscale, a public/Funnel guest) MUST return False so they
    fall through to the STUN/TURN relay tier instead of getting an empty
    ice_servers list. A Funnel-proxied loopback request and any public IP are
    OFF-tailnet; only tailnet CGNAT (100.64.0.0/10), RFC1918 LAN, or IPv6 ULA
    count as on-tailnet.
    """
    ip = _real_client_ip(request)
    if ip is None:
        return False  # no usable client info -> don't assume tailnet
    if ip.is_loopback:
        return False  # loopback (incl. Funnel-proxied) is never treated as tailnet
    return any(ip in net for net in _ONTAILNET_NETS)


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
    peer_fqid = _resolve_peer(peer)  # 404 if not paired (resolve first)
    if not _have_creds():  # 503 only once the peer is valid
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
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        topic = (body.get("topic") or "").strip()
        ctx = _prepare_call(peer)
        _send_invite(
            from_fqid=ctx["identity"],
            to_fqid=ctx["peer_fqid"],
            room=ctx["room"],
            livekit_url=ctx["livekit_url"],
            topic=topic,
        )
        _alert_operator(
            from_fqid=ctx["identity"], to_fqid=ctx["peer_fqid"], room=ctx["room"], topic=topic
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
            if not getattr(_verify, "valid", False):
                continue  # drop unsigned/invalid-signature envelopes
            if getattr(env, "subject", None) != CALL_INVITE_SUBJECT:
                continue
            if getattr(env, "to_fqid", None) != me:
                continue  # never trust an invite not addressed to us
            try:
                inv = parse_invite_body(env.body)
            except ValueError:
                continue
            # The body's from_fqid is the sender's own claim (set client-side in
            # build_invite_body); it is NOT independently attested by skcomms.
            # Cross-check it against the cryptographically verified envelope
            # sender so a paired-but-different peer can't spoof "chef is calling"
            # by forging the JSON payload while sending a validly-signed envelope.
            env_from = getattr(env, "from_fqid", None)
            if inv.get("from_fqid") != env_from:
                logger.warning(
                    "dropping CALL_INVITE with spoofed from_fqid: body=%r envelope=%r",
                    inv.get("from_fqid"),
                    env_from,
                )
                continue
            invites.append(inv)
        invites.sort(key=lambda i: i.get("ts", 0), reverse=True)
        return JSONResponse({"invites": invites})

    @app.get("/call/peers")
    async def call_peers() -> JSONResponse:
        """List paired peers (FQID + fingerprint) for the call UI."""
        peers = [
            {"fqid": fqid, "fingerprint": (meta or {}).get("fingerprint")}
            for fqid, meta in _list_peers().items()
        ]
        return JSONResponse({"peers": peers})

    @app.get("/connectivity/ice")
    async def connectivity_ice(peer: str, request: Request) -> JSONResponse:
        peer_fqid = _resolve_peer(peer)
        local_fqid = _self_fqid()
        # Derive on_tailnet from the actual requesting connection (see
        # _client_on_tailnet) instead of hardcoding it. An off-tailnet caller
        # (mobile data, home wifi w/o Tailscale, a public/Funnel guest) must
        # fall through to the STUN/TURN relay tier or its media never connects.
        on_tailnet = _client_on_tailnet(request)
        cfg = ice_config(local_fqid, peer_fqid, peer_hint={"on_tailnet": on_tailnet})
        return JSONResponse(cfg)
