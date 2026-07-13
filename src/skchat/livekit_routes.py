"""LiveKit routes for skchat — token mint + room helper for video sessions.

skchat does NOT host an SFU. It mints short-lived JWTs that browser/agent
clients use to connect directly to a livekit-server (over the tailnet).

Endpoints:
    GET  /livekit/config        — public client config (URL, identity hint)
    POST /livekit/token         — mint a JWT for {room, identity, name}
    GET  /livekit               — browser video page (Lumina + peers)
    GET  /livekit/{room}        — browser video page joined to a specific room

HLS egress (TV-casting Sprint 1) — turn a room (a shared screen) into a plain
HLS stream with a reachable URL:
    POST /livekit/hls/start     — start a RoomComposite HLS egress {room}
    POST /livekit/hls/stop      — stop an egress {egress_id}
    GET  /livekit/hls/status    — list active HLS egresses
    GET  /hls/{room}/{file}     — PUBLIC media proxy to the .41 segment store

The egress itself runs on .41 (headless Chrome + GStreamer, CPU heavy) and
writes HLS segments to a disk dir mounted into the egress container (host
``~/.skchat/hls`` -> container ``/out/hls``). A small static file server on .41
(``sk-hls-http`` nginx on the tailnet, ``SKCHAT_HLS_ORIGIN``) serves that dir;
the webui on .158 proxies ``/hls/<room>/...`` to it so the playlist + segments
are reachable off-tailnet over the .158 Tailscale Funnel on 443.

Required env (with sane defaults for local-tailnet single-host setups):
    SKCHAT_LIVEKIT_URL         ws://skworld-100:7880
    SKCHAT_LIVEKIT_API_KEY     dev-key
    SKCHAT_LIVEKIT_API_SECRET  dev-secret-change-me
    SKCHAT_LIVEKIT_DEFAULT_ROOM   lumina-and-chef

HLS egress env (defaults match the live .158/.41 tailnet stack):
    SKCHAT_LIVEKIT_API_URL     http://100.108.59.57:7880   (SFU Twirp API)
    SKCHAT_HLS_EGRESS_DIR      /out/hls                     (container-side dir)
    SKCHAT_HLS_ORIGIN          http://100.86.156.5:8099     (.41 static server)
    SKCHAT_HLS_PUBLIC_BASE     (falls back to SKCHAT_FUNNEL_PUBLIC_URL)
    SKCHAT_HLS_SEGMENT_DURATION  6
    SKCHAT_HLS_LAYOUT          grid

If livekit-api isn't installed the routes return 503 with a clear hint —
the rest of skchat keeps working.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

logger = logging.getLogger("skchat.livekit_routes")

LIVEKIT_URL = os.getenv("SKCHAT_LIVEKIT_URL", "ws://skworld-100:7880")
LIVEKIT_API_KEY = os.getenv("SKCHAT_LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("SKCHAT_LIVEKIT_API_SECRET", "")
DEFAULT_ROOM = os.getenv("SKCHAT_LIVEKIT_DEFAULT_ROOM", "lumina-and-chef")
DEFAULT_TTL_SECONDS = int(os.getenv("SKCHAT_LIVEKIT_TOKEN_TTL", "21600"))  # 6h

# Public/Funnel-reachable SFU wss URL. The tailnet LIVEKIT_URL (:8443) is only
# reachable by peers already on the tailnet; a public guest arriving via
# Tailscale Funnel must be handed this URL instead. Read at call time so the
# helper and tests stay env-driven (see ``public_aware_livekit_url``).
LIVEKIT_PUBLIC_URL_ENV = "SKCHAT_LIVEKIT_PUBLIC_URL"
# Optional explicit tailnet host override. When unset, the tailnet host is
# derived from ``SKCHAT_LIVEKIT_URL``'s hostname.
TAILNET_HOST_ENV = "SKCHAT_TAILNET_HOST"


def _have_creds() -> bool:
    return bool(LIVEKIT_API_KEY and LIVEKIT_API_SECRET)


def _request_room_token(request: object, body: dict | None) -> str:
    """Extract a LiveKit token from the request (body ``token`` or Bearer header)."""
    if isinstance(body, dict):
        tok = (body.get("token") or "").strip()
        if tok:
            return tok
    headers = getattr(request, "headers", {}) or {}
    auth = (headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _token_room(token: str) -> str | None:
    """Return the ``video.room`` claim of a validly-signed LiveKit token, else None.

    Verifies the signature + expiry with our own API secret, so a forged or
    stale token yields None.
    """
    if not token or not _have_creds():
        return None
    try:
        from livekit import api  # soft dep

        claims = api.TokenVerifier(LIVEKIT_API_KEY, LIVEKIT_API_SECRET).verify(token)
        video = getattr(claims, "video", None)
        return getattr(video, "room", None) if video is not None else None
    except Exception:
        logger.debug("hls: room-token verify failed", exc_info=True)
        return None


def _hls_authorized(request: object, room: str, body: dict | None) -> bool:
    """Authorize an HLS control action for ``room``.

    Allowed if EITHER the caller is operator/loopback/tailnet (control plane),
    OR the caller presents a valid LiveKit token whose ``video.room`` matches
    ``room`` (i.e. a participant already in the room). The second path lets a
    Space host / call participant start casting from their phone over the public
    Funnel, where they are neither loopback/tailnet nor an operator, while
    staying DoS-safe: you can only start the egress for a room you can join.
    """
    try:
        _gate_token_mint(request)
        return True
    except HTTPException:
        pass
    return bool(room) and _token_room(_request_room_token(request, body)) == room


def _gate_token_mint(request: object) -> None:
    """Gate POST /livekit/token to loopback/tailnet OR an operator token.

    ``_mint_token`` issues a FULL-publish JWT for any caller-supplied
    ``identity``, so this endpoint must NOT be reachable by anonymous public
    callers (e.g. over Tailscale Funnel) — that would be an impersonation hole.

    We reuse the *exact same* gate the guest operator endpoints use
    (``skchat.guest._require_operator``):
      * If ``SKCHAT_GUEST_OPERATOR_TOKEN`` is set, the caller MUST present it
        (``Authorization: Bearer <token>`` or ``X-Operator-Token``).
      * Otherwise, only loopback (``127.``/``::1``) or private/tailnet IPs
        (RFC1918 ``10.``/``192.168.``, Tailscale CGNAT ``100.64.0.0/10`` →
        ``100.``, ULA ``fd``) are allowed.
      * Anything else → HTTP 401/403.

    This keeps lumina-call.py (localhost ``WEBUI_URL``) and tailnet browser
    callers working while blocking public/Funnel callers from minting
    arbitrary-identity tokens. The conf join path uses /conf/{room}/token and
    /join/sovereign instead (the safe public paths).

    The import is DRY (single source of truth in guest.py); a tiny local
    mirror is used only if guest.py can't be imported.
    """
    try:
        from skchat.guest import _require_operator
    except Exception:  # pragma: no cover - defensive fallback if guest.py shifts
        _require_operator = _local_require_operator
    _require_operator(request)


# Private/loopback prefixes mirroring skchat.guest._PRIVATE_PREFIXES. Only used
# by the fallback below if guest.py's _require_operator can't be imported.
_PRIVATE_PREFIXES = (
    "127.",
    "10.",
    "192.168.",
    "100.",  # Tailscale CGNAT range (100.64.0.0/10)
    "::1",
    "fd",  # ULA / Tailscale IPv6
)


def _local_require_operator(request: object) -> None:
    """Fallback operator gate mirroring ``skchat.guest._require_operator``.

    Only reached if guest.py is not importable. Same policy: operator bearer
    token when ``SKCHAT_GUEST_OPERATOR_TOKEN`` is set, else loopback/tailnet
    private-IP only.
    """
    import secrets

    from fastapi import HTTPException

    token = os.getenv("SKCHAT_GUEST_OPERATOR_TOKEN", "").strip()
    if token:
        headers = getattr(request, "headers", {}) or {}
        presented = (headers.get("x-operator-token") or "").strip()
        if not presented:
            auth = (headers.get("authorization") or "").strip()
            if auth.lower().startswith("bearer "):
                presented = auth[7:].strip()
        if not secrets.compare_digest(presented, token):
            raise HTTPException(status_code=401, detail="operator authentication required")
        return
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client is not None else None
    if not host or not host.startswith(_PRIVATE_PREFIXES):
        raise HTTPException(
            status_code=403,
            detail="token mint is tailnet-only; set SKCHAT_GUEST_OPERATOR_TOKEN "
            "to allow authenticated remote access",
        )


def _request_host(request: object) -> str:
    """Return the request's effective host (lowercased, no port).

    Prefers ``X-Forwarded-Host`` (set by Tailscale Funnel / reverse proxies)
    and falls back to ``Host``. Returns ``""`` when neither is present.
    """
    headers = getattr(request, "headers", None) or {}
    raw = headers.get("x-forwarded-host") or headers.get("host") or ""
    # X-Forwarded-Host may carry a comma list (proxy chain) — take the first.
    raw = raw.split(",")[0].strip().lower()
    # Strip a trailing :port if present.
    if raw.startswith("["):  # bracketed IPv6 literal, e.g. [::1]:8443
        end = raw.find("]")
        return raw[: end + 1] if end != -1 else raw
    return raw.split(":", 1)[0]


def _tailnet_host() -> str:
    """The host that identifies tailnet-origin requests (lowercased, no port).

    Explicit ``SKCHAT_TAILNET_HOST`` wins; otherwise derive from the hostname
    of ``SKCHAT_LIVEKIT_URL``.
    """
    explicit = os.getenv(TAILNET_HOST_ENV, "").strip().lower()
    if explicit:
        return explicit.split(":", 1)[0]
    url = os.getenv("SKCHAT_LIVEKIT_URL", LIVEKIT_URL)
    return (urlsplit(url).hostname or "").lower()


# Address ranges that count as genuinely on-tailnet / on-LAN. This is the SINGLE
# source of truth: ``call_routes`` imports ``_ONTAILNET_NETS`` / ``_real_client_ip``
# from here, so the public-aware URL selection and /connectivity/ice can never
# disagree about who is on the tailnet. Loopback is DELIBERATELY excluded: this
# host sits behind Tailscale Funnel, which terminates TLS locally and forwards to
# the app over loopback, so a genuine off-tailnet phone on cellular arrives as
# request.client.host == 127.0.0.1.
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


def _real_client_ip(request: object) -> ipaddress._BaseAddress | None:
    """Resolve the true client IP, honoring reverse-proxy / Tailscale Funnel headers.

    Canonical implementation (``call_routes`` imports this): the direct socket peer
    (``request.client.host``) is not trustworthy on this host because Tailscale
    Funnel and any reverse proxy terminate the connection locally and forward to
    the app over loopback. When the direct peer is loopback (a local proxy) or
    the request carries Funnel/forwarding signals, we honor the leftmost
    ``X-Forwarded-For`` entry (the original client). A direct, non-loopback peer
    with no forwarding is trusted as-is.
    """
    headers = getattr(request, "headers", None) or {}
    client = getattr(request, "client", None)
    direct_ip = _parse_ip(getattr(client, "host", None) if client is not None else None)

    proxied = direct_ip is not None and direct_ip.is_loopback
    funnel = bool(headers.get("tailscale-funnel-request"))
    xff = headers.get("x-forwarded-for") or ""

    if (proxied or funnel) and xff:
        forwarded = _parse_ip(xff.split(",")[0])
        if forwarded is not None:
            return forwarded
    return direct_ip


def _request_off_tailnet(request: object) -> bool:
    """True only when the request demonstrably arrived from OFF the tailnet.

    This is the connection-layer signal that host-header matching cannot see:
    when the public Funnel host and the tailnet host share a hostname (differing
    only by port, e.g. ``noroc2027.tail204f0c.ts.net`` on public :443 vs tailnet
    :8443), the Host header is identical for both, so we MUST fall back to the
    ingress signal to tell a cellular guest apart from a tailnet peer.

    Positive off-tailnet signals:
      * the Tailscale Funnel ingress stamped the request
        (``Tailscale-Funnel-Request``): by definition a public ingress with no
        tailnet identity; or
      * the resolved real client IP is a PUBLIC address (not loopback and not in
        any tailnet/LAN range).

    Returns False when there is no positive signal (a genuine tailnet/LAN
    caller, a bare loopback local caller, or no client info) so the caller can
    fall back to host-header discrimination.
    """
    headers = getattr(request, "headers", None) or {}
    if headers.get("tailscale-funnel-request"):
        return True
    ip = _real_client_ip(request)
    if ip is None or ip.is_loopback:
        return False
    return not any(ip in net for net in _ONTAILNET_NETS)


def public_aware_livekit_url(request: object) -> str:
    """Return the SFU wss URL appropriate for the requesting client.

    Behavior:
      * If ``SKCHAT_LIVEKIT_PUBLIC_URL`` is unset/empty -> always the tailnet
        ``SKCHAT_LIVEKIT_URL`` (identical to pre-public behavior).
      * If set, and the request demonstrably arrived from OFF the tailnet
        (Tailscale Funnel ingress, or a public real client IP -- see
        ``_request_off_tailnet``) -> the public URL. This is the robust signal
        that works even when the public Funnel host and the tailnet host share a
        hostname and differ only by port (:443 vs :8443), which is exactly the
        .158 Funnel deployment: a cellular phone cannot reach the tailnet :8443
        SFU, so it MUST be handed the public wss URL.
      * If set, and the request's Host / X-Forwarded-Host does NOT match the
        tailnet host -> the request arrived over a distinct public host, so
        return the public URL (kept for name-based split deployments).
      * Otherwise (host matches the tailnet host, or no host header, i.e. a
        genuine local/tailnet caller) -> the tailnet URL.

    Read env at call time so a long-running process and the tests share one
    code path.
    """
    tailnet_url = os.getenv("SKCHAT_LIVEKIT_URL", LIVEKIT_URL)
    public_url = os.getenv(LIVEKIT_PUBLIC_URL_ENV, "").strip()
    if not public_url:
        return tailnet_url

    # 1) Connection-layer signal: robust even when public + tailnet hosts share a
    #    hostname (Funnel :443 vs tailnet :8443). A Funnel-proxied cellular guest
    #    is caught here regardless of its (tailnet-looking) Host header.
    if _request_off_tailnet(request):
        return public_url

    # 2) Host-header discrimination: for deployments where the public host has a
    #    distinct name from the tailnet host.
    req_host = _request_host(request)
    if not req_host:
        # No host header -> treat as a local/tailnet caller (unchanged default).
        return tailnet_url
    if req_host == _tailnet_host():
        return tailnet_url
    return public_url


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


# ── HLS egress helpers (TV-casting Sprint 1) ─────────────────────────────────
# All env is read at call time so a long-running webui and the tests share one
# code path (mirrors ``public_aware_livekit_url``).

_ROOM_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_FILE_SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _safe_room(room: str) -> str:
    """Sanitize a room name to a path-safe token (no traversal, no slashes)."""
    room = (room or "").strip()
    room = _ROOM_SAFE_RE.sub("-", room).strip("-.")
    return room[:120]


def _livekit_api_url() -> str:
    """HTTP(S) base URL of the LiveKit Twirp API used to drive egress.

    Explicit ``SKCHAT_LIVEKIT_API_URL`` wins. Otherwise derive a ``host:port``
    base from ``SKCHAT_LIVEKIT_URL`` — but only when that URL actually carries a
    port (a bare ``ws://host:7880``). A Funnel-style URL (``wss://host/path``
    with no port) is NOT a usable API endpoint, so we fall back to the tailnet
    default SFU API port.
    """
    explicit = os.getenv("SKCHAT_LIVEKIT_API_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    url = os.getenv("SKCHAT_LIVEKIT_URL", LIVEKIT_URL)
    parts = urlsplit(url)
    if parts.hostname and parts.port:
        scheme = "https" if parts.scheme in ("wss", "https") else "http"
        return f"{scheme}://{parts.hostname}:{parts.port}"
    # Funnel /path URL with no port -> not an API endpoint; use tailnet default.
    return "http://100.108.59.57:7880"


def _hls_egress_dir() -> str:
    """Container-side base dir the egress writes HLS output into."""
    return os.getenv("SKCHAT_HLS_EGRESS_DIR", "/out/hls").rstrip("/")


def _hls_origin() -> str:
    """Base URL of the .41 static file server that serves the HLS dir."""
    return os.getenv("SKCHAT_HLS_ORIGIN", "http://100.86.156.5:8099").rstrip("/")


def _hls_public_base() -> str:
    """Public base (behind the .158 Funnel) used to build the returned hls_url.

    Falls back to ``SKCHAT_FUNNEL_PUBLIC_URL`` (already set in the webui env),
    then to an empty string (relative URL) for local/test callers.
    """
    base = os.getenv("SKCHAT_HLS_PUBLIC_BASE", "").strip()
    if not base:
        base = os.getenv("SKCHAT_FUNNEL_PUBLIC_URL", "").strip()
    return base.rstrip("/")


def _hls_url(room: str) -> str:
    """Public playlist URL for a room's HLS stream (``.../hls/<room>/index.m3u8``)."""
    return f"{_hls_public_base()}/hls/{room}/index.m3u8"


def _livekit_api_client():
    """Construct a LiveKit API client. Single seam the tests monkeypatch.

    Raises ``ImportError`` if livekit-api isn't installed (handled by callers).
    """
    from livekit import api  # local import — soft dep

    return api.LiveKitAPI(_livekit_api_url(), LIVEKIT_API_KEY, LIVEKIT_API_SECRET)


async def _egress_start_hls(room: str, *, segment_duration: int, layout: str) -> dict:
    """Start a RoomComposite egress writing a segmented HLS playlist for ``room``.

    Writes ``<dir>/<room>/index.m3u8`` (+ a bounded ``live.m3u8``) and
    ``segment_*.ts`` files into the egress container's mounted dir. Returns
    ``{egress_id, status}``.
    """
    from livekit import api  # local import — soft dep

    base = _hls_egress_dir()
    seg = api.SegmentedFileOutput(
        protocol=api.SegmentedFileProtocol.HLS_PROTOCOL,
        filename_prefix=f"{base}/{room}/segment",
        playlist_name=f"{base}/{room}/index.m3u8",
        live_playlist_name=f"{base}/{room}/live.m3u8",
        segment_duration=int(segment_duration),
    )
    req = api.RoomCompositeEgressRequest(
        room_name=room,
        layout=layout,
        audio_only=False,
        segment_outputs=[seg],
    )
    lk = _livekit_api_client()
    try:
        info = await lk.egress.start_room_composite_egress(req)
    finally:
        await lk.aclose()
    return {
        "egress_id": info.egress_id,
        "status": api.EgressStatus.Name(info.status),
    }


async def _egress_stop(egress_id: str) -> dict:
    """Stop an egress by id. Returns ``{egress_id, status}``."""
    from livekit import api  # local import — soft dep

    lk = _livekit_api_client()
    try:
        info = await lk.egress.stop_egress(api.StopEgressRequest(egress_id=egress_id))
    finally:
        await lk.aclose()
    return {
        "egress_id": info.egress_id,
        "status": api.EgressStatus.Name(info.status),
    }


async def _egress_list_active() -> list[dict]:
    """List currently active egresses as ``[{egress_id, room, status}]``."""
    from livekit import api  # local import — soft dep

    lk = _livekit_api_client()
    try:
        resp = await lk.egress.list_egress(api.ListEgressRequest(active=True))
    finally:
        await lk.aclose()
    out: list[dict] = []
    for it in resp.items:
        out.append(
            {
                "egress_id": it.egress_id,
                "room": it.room_name,
                "status": api.EgressStatus.Name(it.status),
            }
        )
    return out


def register_livekit_routes(app: FastAPI) -> None:
    """Register LiveKit endpoints on the FastAPI app."""

    @app.get("/livekit/config")
    async def livekit_config(request: Request) -> JSONResponse:
        """Browser-safe config; never includes the API secret.

        The SFU ``url`` is request-aware: a public/Funnel guest gets the public
        wss URL, a tailnet client gets the tailnet URL (see
        ``public_aware_livekit_url``).
        """
        return JSONResponse(
            {
                "url": public_aware_livekit_url(request),
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

        Access gate: callable ONLY from loopback/tailnet OR with a valid
        operator token (``SKCHAT_GUEST_OPERATOR_TOKEN``). Public/Funnel callers
        are rejected (401/403) before any token is minted — this route hands out
        FULL-publish JWTs for an arbitrary ``identity``, so it must not be
        anonymously reachable. Public conf joins use /conf/{room}/token instead.
        """
        _gate_token_mint(request)
        if not _have_creds():
            raise HTTPException(
                status_code=503,
                detail="livekit not configured: set SKCHAT_LIVEKIT_API_KEY/SECRET",
            )

        try:
            body = await request.json()
        except Exception as e:
            logger.warning("livekit_routes.py: %s", e)
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
                "url": public_aware_livekit_url(request),
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

        body = (
            await request.json()
            if request.headers.get("content-type", "").startswith("application/json")
            else dict(await request.form())
        )
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

    # ── HLS egress endpoints (TV-casting Sprint 1) ───────────────────────
    # Start/stop a RoomComposite HLS egress for a room and hand back a plain
    # HLS URL a TV / cast receiver can play. The egress runs on .41; the
    # playlist + segments are proxied back through this webui (on the .158
    # Funnel) by the PUBLIC /hls/{room}/{file} route below.

    @app.post("/livekit/hls/start")
    async def livekit_hls_start(request: Request) -> JSONResponse:
        """Start an HLS egress for ``{room}`` and return ``{egress_id, hls_url}``.

        Gated exactly like /livekit/token (loopback/tailnet or operator token):
        starting an egress is a control action, not something a public/Funnel
        caller should trigger.
        """
        if not _have_creds():
            raise HTTPException(
                status_code=503,
                detail="livekit not configured: set SKCHAT_LIVEKIT_API_KEY/SECRET",
            )
        body = (
            await request.json()
            if request.headers.get("content-type", "").startswith("application/json")
            else dict(await request.form())
        )
        room = _safe_room(body.get("room") or DEFAULT_ROOM)
        if not room:
            raise HTTPException(status_code=400, detail="room required")
        # Authorize AFTER we know the room: operator/tailnet OR a valid LiveKit
        # token for THIS room. The token path lets a Space host / call participant
        # start casting from their phone over the Funnel (they hold a room token
        # but are not loopback/tailnet/operator).
        if not _hls_authorized(request, room, body):
            raise HTTPException(
                status_code=403,
                detail="not authorized to start HLS for this room",
            )

        # Room-scoped single-egress guard: if an HLS egress is already active for
        # this room, reuse it instead of starting a second. Each RoomComposite
        # egress is CPU heavy on .41 (headless Chrome + GStreamer), so two people
        # both tapping "Cast to TV" for the same room must NOT double-start. This
        # makes the endpoint idempotent per room: the second caller gets the same
        # egress_id + hls_url the first caller started. Best-effort only: any list
        # failure (SFU hiccup, livekit-api missing) falls through to a fresh start
        # so casting is never blocked by the guard.
        try:
            for eg in await _egress_list_active():
                if eg.get("room") == room:
                    return JSONResponse(
                        {
                            "ok": True,
                            "egress_id": eg["egress_id"],
                            "status": eg["status"],
                            "room": room,
                            "hls_url": _hls_url(room),
                            "playlist": f"/hls/{room}/index.m3u8",
                            "reused": True,
                        }
                    )
        except HTTPException:
            raise
        except Exception:
            logger.debug("hls pre-start list failed; starting fresh", exc_info=True)

        try:
            segment_duration = int(
                body.get("segment_duration")
                or os.getenv("SKCHAT_HLS_SEGMENT_DURATION", "6")
            )
        except (TypeError, ValueError):
            segment_duration = 6
        layout = (body.get("layout") or os.getenv("SKCHAT_HLS_LAYOUT", "grid")).strip() or "grid"

        try:
            result = await _egress_start_hls(
                room, segment_duration=segment_duration, layout=layout
            )
        except ImportError:
            raise HTTPException(
                status_code=503,
                detail="livekit-api not installed: pip install livekit-api",
            )
        except Exception as exc:
            logger.exception("hls egress start failed")
            raise HTTPException(status_code=502, detail=f"egress start failed: {exc}") from exc

        return JSONResponse(
            {
                "ok": True,
                "egress_id": result["egress_id"],
                "status": result["status"],
                "room": room,
                "hls_url": _hls_url(room),
                "playlist": f"/hls/{room}/index.m3u8",
                "reused": False,
            }
        )

    @app.post("/livekit/hls/stop")
    async def livekit_hls_stop(request: Request) -> JSONResponse:
        """Stop an HLS egress by ``{egress_id}``.

        Authorized like /start: operator/tailnet OR a valid LiveKit token for
        the egress's room (client sends ``{egress_id, room, token}``). On the
        token path we additionally confirm the egress actually belongs to that
        room so a room token cannot stop another room's egress.
        """
        if not _have_creds():
            raise HTTPException(status_code=503, detail="livekit not configured")
        body = (
            await request.json()
            if request.headers.get("content-type", "").startswith("application/json")
            else dict(await request.form())
        )
        egress_id = (body.get("egress_id") or "").strip()
        if not egress_id:
            raise HTTPException(status_code=400, detail="egress_id required")
        room = _safe_room(body.get("room") or "")
        authorized = False
        try:
            _gate_token_mint(request)
            authorized = True
        except HTTPException:
            authorized = False
        if not authorized and room and _token_room(_request_room_token(request, body)) == room:
            # Valid token for `room`; confirm the egress belongs to it (best-effort:
            # a list failure does not block a legitimate stop).
            try:
                authorized = any(
                    eg.get("egress_id") == egress_id and eg.get("room") == room
                    for eg in await _egress_list_active()
                )
            except Exception:
                authorized = True
        if not authorized:
            raise HTTPException(
                status_code=403, detail="not authorized to stop this egress"
            )
        try:
            result = await _egress_stop(egress_id)
        except ImportError:
            raise HTTPException(status_code=503, detail="livekit-api not installed")
        except Exception as exc:
            logger.exception("hls egress stop failed")
            raise HTTPException(status_code=502, detail=f"egress stop failed: {exc}") from exc
        return JSONResponse({"ok": True, **result})

    @app.get("/livekit/hls/status")
    async def livekit_hls_status(request: Request) -> JSONResponse:
        """List active HLS egresses (tailnet/operator-gated)."""
        _gate_token_mint(request)
        if not _have_creds():
            raise HTTPException(status_code=503, detail="livekit not configured")
        try:
            egresses = await _egress_list_active()
        except ImportError:
            raise HTTPException(status_code=503, detail="livekit-api not installed")
        except Exception as exc:
            logger.exception("hls egress status failed")
            raise HTTPException(status_code=502, detail=f"egress status failed: {exc}") from exc
        return JSONResponse({"egresses": egresses})

    # ── PUBLIC HLS media proxy ───────────────────────────────────────────
    # Proxies the playlist + segments from the .41 static server so an
    # off-tailnet TV / cast receiver can fetch them over the .158 Funnel on
    # 443. Deliberately UNGATED (a cast receiver has no tailnet identity), with
    # permissive CORS and correct HLS content types. Only simple filenames
    # under a sanitized room dir are allowed (no path traversal).

    def _hls_content_type(name: str) -> str:
        if name.endswith(".m3u8"):
            return "application/vnd.apple.mpegurl"
        if name.endswith(".ts"):
            return "video/mp2t"
        if name.endswith(".mp4"):
            return "video/mp4"
        return "application/octet-stream"

    _HLS_CORS = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "*",
        "Cache-Control": "no-cache",
    }

    @app.options("/hls/{room}/{filename}")
    async def hls_media_preflight(room: str, filename: str) -> Response:  # noqa: ARG001
        return Response(status_code=204, headers=_HLS_CORS)

    @app.get("/hls/{room}/{filename}")
    async def hls_media(room: str, filename: str) -> Response:
        room = _safe_room(room)
        if not room or not _FILE_SAFE_RE.match(filename):
            raise HTTPException(status_code=404, detail="not found")
        url = f"{_hls_origin()}/{room}/{filename}"
        import aiohttp  # local import — soft dep (already pulled in by livekit)

        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        raise HTTPException(
                            status_code=404 if resp.status in (403, 404) else 502,
                            detail="hls asset unavailable",
                        )
                    data = await resp.read()
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("hls proxy fetch failed: %s", exc)
            raise HTTPException(status_code=502, detail="hls origin unreachable") from exc
        headers = dict(_HLS_CORS)
        return Response(content=data, media_type=_hls_content_type(filename), headers=headers)

    # ── Recording endpoints ──────────────────────────────────────────────
    # Webui-driven capture of Lumina's audio track to a WAV file in
    # ~/.skchat/lumina-recordings/. Spawns lumina_recorder.py as a subprocess
    # — it joins the room, subscribes to the target's audio track, writes
    # frames to disk. Stop endpoint sends SIGTERM for clean WAV close.

    @app.post("/livekit/record/start")
    async def livekit_record_start(request: Request) -> JSONResponse:
        body = (
            await request.json()
            if request.headers.get("content-type", "").startswith("application/json")
            else dict(await request.form())
        )
        target = (body.get("target") or "lumina").strip()
        room_name = body.get("room") or DEFAULT_ROOM
        label = (body.get("label") or "recording").strip().replace(" ", "_")[:60] or "recording"

        rec_dir = Path.home() / ".skchat" / "lumina-recordings"
        rec_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_path = rec_dir / f"{stamp}_{target}_{label}.wav"

        # Spawn the recorder as a child process under the same user. It auto-
        # exits cleanly on SIGTERM. We track PID + path in a ledger file.
        ledger = rec_dir / ".active.json"
        cmd = [
            sys.executable,
            "-m",
            "skchat.lumina_recorder",
            "--out",
            str(out_path),
            "--room",
            room_name,
            "--target",
            target,
            "--webui",
            "https://REDACTED-TAILSCALE-HOST",
        ]
        log_path = rec_dir / f"{stamp}_{target}_{label}.log"
        log_fh = log_path.open("w")
        proc = subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT)
        ledger.write_text(
            json.dumps(
                {
                    "pid": proc.pid,
                    "path": str(out_path),
                    "log": str(log_path),
                    "target": target,
                    "room": room_name,
                    "label": label,
                    "started_at": datetime.now().isoformat(),
                }
            ),
            encoding="utf-8",
        )
        return JSONResponse(
            {"ok": True, "pid": proc.pid, "path": str(out_path), "log": str(log_path)}
        )

    @app.post("/livekit/record/stop")
    async def livekit_record_stop() -> JSONResponse:
        rec_dir = Path.home() / ".skchat" / "lumina-recordings"
        ledger = rec_dir / ".active.json"
        if not ledger.exists():
            return JSONResponse({"ok": False, "reason": "no active recording"}, status_code=404)
        info = json.loads(ledger.read_text(encoding="utf-8"))
        pid = int(info["pid"])
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        # Wait briefly for clean WAV close.
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        ledger.unlink(missing_ok=True)
        path = Path(info.get("path", ""))
        size = path.stat().st_size if path.exists() else 0
        return JSONResponse({"ok": True, "path": str(path), "size_bytes": size})

    @app.get("/recordings")
    async def list_recordings() -> JSONResponse:
        rec_dir = Path.home() / ".skchat" / "lumina-recordings"
        rec_dir.mkdir(parents=True, exist_ok=True)
        ledger = rec_dir / ".active.json"
        active = None
        if ledger.exists():
            try:
                active = json.loads(ledger.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("livekit_routes.py: %s", e)
                active = None
        out = []
        for p in sorted(rec_dir.glob("*.wav"), reverse=True):
            stat = p.stat()
            out.append(
                {
                    "name": p.name,
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "url": f"/recordings/{p.name}",
                }
            )
        return JSONResponse({"recordings": out, "active": active})

    @app.get("/recordings/{name}")
    async def fetch_recording(name: str) -> FileResponse:
        rec_dir = Path.home() / ".skchat" / "lumina-recordings"
        # basic path-traversal guard
        path = (rec_dir / name).resolve()
        if not str(path).startswith(str(rec_dir.resolve())) or not path.exists():
            raise HTTPException(status_code=404, detail="recording not found")
        return FileResponse(path, media_type="audio/wav", filename=name)

    @app.get("/livekit", response_class=HTMLResponse)
    async def livekit_page() -> HTMLResponse:
        return _serve_livekit_html()

    @app.get("/livekit/{room}", response_class=HTMLResponse)
    async def livekit_room_page(room: str) -> HTMLResponse:  # noqa: ARG001
        return _serve_livekit_html()

    @app.get("/recordings.html", response_class=HTMLResponse)
    async def recordings_page() -> HTMLResponse:
        static = Path(__file__).parent / "static" / "recordings.html"
        if static.exists():
            return FileResponse(static, media_type="text/html")
        return HTMLResponse("recordings.html missing", status_code=500)


def _serve_livekit_html() -> HTMLResponse:
    static = Path(__file__).parent / "static" / "livekit.html"
    if static.exists():
        return FileResponse(static, media_type="text/html")
    return HTMLResponse(
        "<h1>livekit.html missing</h1><p>Expected at "
        f"{static}. Run scripts/install-livekit-page.sh.</p>",
        status_code=500,
    )
