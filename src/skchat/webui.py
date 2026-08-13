"""SKChat Web UI — minimal FastAPI + HTMX chat interface.

Usage:  skchat webui [--port 8765] [--no-browser]
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re as _re
import uuid as _uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)

from . import __version__
from .dataplane_auth import (
    audience_mint_enabled,
    dataplane_auth_enabled,
    enforce_dataplane_auth,
    request_is_authenticated,
    request_is_primary_authenticated,
    require_dataplane_auth,
)
from .dataplane_paths import is_gated
from .embed_auth import (
    EMBED_MODULES,
    MODE_RO,
    MODE_RW,
    EmbedAuthError,
    cookie_name,
    cookie_path,
    embed_tokens_enabled,
    mint_embed_token,
    presented_via_query,
    request_embed_token,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="SKChat Web UI")


@app.middleware("http")
async def _operator_auth_gate(request, call_next):
    """Method+path-aware operator-auth gate (dark, flag OFF by default).

    Flag OFF -> `dataplane_auth_enabled()` is False, this is a pure passthrough
    (zero behavior change to the live daemon). Flag ON -> gated requests
    (per `is_gated`) must carry a valid operator-session JWT or capauth
    assertion, or the request is refused with a 401 before reaching the route.

    WEBSOCKET BOUNDARY: this is an ``@app.middleware("http")`` gate, it does
    NOT cover websocket routes (``/ws/*``, e.g. ``/ws/chat`` below). See the
    module docstring in ``dataplane_paths.py`` for the full boundary note and
    why that is currently low-risk (refresh-signal metadata only, no content).
    """
    if dataplane_auth_enabled() and is_gated(request.method, request.url.path):
        try:
            enforce_dataplane_auth(request)
        except Exception:
            return JSONResponse({"detail": "capauth authentication required"}, status_code=401)
    return await call_next(request)


@app.middleware("http")
async def _debug_log_app_requests(request, call_next):
    """TEMP diagnostic: log the raw request the native app makes for sends/calls."""
    import os as _os

    if _os.environ.get("SKCHAT_DEBUG_REQ", "").strip() in ("1", "true", "yes"):
        p = request.url.path
        interesting = (
            request.method in ("POST", "PUT")
            or "call" in p
            or "rtc" in p
            or "livekit" in p
            or "group" in p
        )
        if interesting:
            try:
                body = await request.body()

                async def _receive():
                    return {"type": "http.request", "body": body, "more_body": False}

                request._receive = _receive
                logger.info(
                    "APP-REQ %s %s q=%s body=%s",
                    request.method,
                    p,
                    dict(request.query_params),
                    body.decode("utf-8", "replace")[:400],
                )
            except Exception as _e:  # noqa: BLE001
                logger.info("APP-REQ %s %s (body read err: %s)", request.method, p, _e)
    return await call_next(request)


_SKCHAT_HOME = Path("~/.skchat").expanduser()

# Upload size cap for the /upload endpoint (100 MiB).
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

# Register voice routes — prefer the SKVoice proxy (voice_ws_lite) which delegates
# the full STT/LLM/TTS chain to the skvoice service. Fall back to the legacy
# voice_stream (in-process pipeline) only if the lite module fails to import or
# SKCHAT_VOICE_MODE=local is set.
_voice_mode = os.getenv("SKCHAT_VOICE_MODE", "proxy").lower()
_voice_routes_loaded = False

if _voice_mode != "local":
    try:
        from .voice_ws_lite import register_voice_routes_lite as _register_voice_routes_lite

        _register_voice_routes_lite(app)
        _voice_routes_loaded = True
    except ImportError:
        pass

if not _voice_routes_loaded:
    try:
        from .voice_stream import register_voice_routes as _register_voice_routes

        _register_voice_routes(app)
        _voice_routes_loaded = True
    except ImportError:
        _voice_routes_loaded = False

# FaceTime routes — aiortc/MuseTalk path (existing, fallback for non-LiveKit clients).
try:
    from .facetime import register_facetime_routes as _register_facetime_routes

    _register_facetime_routes(app)
except ImportError:
    pass

# LiveKit routes — primary video stack (token endpoint + room signalling helper).
try:
    from .livekit_routes import register_livekit_routes as _register_livekit_routes

    _register_livekit_routes(app)
except ImportError as _e:
    logger.warning("livekit routes not registered: %s", _e)
try:
    from .call_routes import register_call_routes as _register_call_routes

    _register_call_routes(app)
except ImportError as _e:
    logger.warning("call routes not registered: %s", _e)
try:
    from .spaces.routes import register_spaces_routes as _register_spaces_routes

    _register_spaces_routes(app)
except ImportError as _e:
    logger.warning("spaces routes not registered: %s", _e)
# Local (non-federation) sovereign conference join: POST /join/sovereign verifies
# a capauth-signed FQID assertion + replay-nonce guard, then mints a SOVEREIGN
# conf token whose LiveKit identity is the PROVEN fqid.
try:
    from .join_routes import register_join_routes as _register_join_routes

    _register_join_routes(app)
except ImportError as _e:
    logger.warning("join routes not registered: %s", _e)
try:
    from .conf.routes import register_conf_routes as _register_conf_routes

    _register_conf_routes(app)
except ImportError as _e:
    logger.warning("conf routes not registered: %s", _e)
# Federation observability — read-only GET /federation/status (identity, relays,
# trust policy, pinned peers, discovered focus hosts, live conf/space + token
# counters). Best-effort, never 500.
try:
    from .federation_status import (
        register_federation_status_routes as _register_federation_status_routes,
    )

    _register_federation_status_routes(app)
except ImportError as _e:
    logger.warning("federation status route not registered: %s", _e)
try:
    from .glossa_mesh.routes import register_glossa_routes as _register_glossa_routes

    _register_glossa_routes(app)
except ImportError as _e:
    logger.warning("glossa routes not registered: %s", _e)
# Guest invite routes — one-link ephemeral call access. /join/{room} + /guest/join
# stay public (invite-JWT-gated); /guest/invite + /guest/revoke are operator-gated
# inside guest.py (loopback/tailnet client or SKCHAT_GUEST_OPERATOR_TOKEN).
try:
    from .guest import register_guest_routes as _register_guest_routes

    _register_guest_routes(app)
except ImportError as _e:
    logger.warning("guest routes not registered: %s", _e)

# Guest GROUP access — one-link, group-scoped, full-in-room, UNTRUSTED guests.
# Operator invite mint/list/revoke + guest join + guest-scoped chat/file/call.
# The whole surface is gated behind SKCHAT_GUEST_LINKS_ENABLED (default off →
# operator routes 404, guest routes 403). No public ingress is wired.
try:
    from .guest_group_routes import register_guest_group_routes as _register_guest_group_routes

    _register_guest_group_routes(app)
except ImportError as _e:
    logger.warning("guest group routes not registered: %s", _e)

# Daemon API proxy for the Flutter app
try:
    from .daemon_proxy import router as daemon_api_router

    app.include_router(daemon_api_router)
except ImportError as _e:
    logger.warning("daemon API proxy not registered: %s", _e)

# Operator device-key auth handshake (/api/v1/auth/*). Ships dark: routes are
# live but nothing is gated on their output until the enforcement middleware
# is added in a later task.
try:
    from .operator_auth import DeviceStore as _DeviceStore
    from .operator_auth import default_device_store_path as _default_device_store_path
    from .operator_auth_routes import (
        register_operator_auth_routes as _register_operator_auth_routes,
    )

    _operator_device_store = _DeviceStore(_default_device_store_path())
    _register_operator_auth_routes(app, device_store=_operator_device_store)

    from .device_routes import register_device_routes as _register_device_routes

    _register_device_routes(app, device_store=_operator_device_store)
except ImportError as _e:
    logger.warning("operator auth routes not registered: %s", _e)


@app.get("/")
async def root() -> RedirectResponse:
    """Redirect to the Flutter app."""
    return RedirectResponse(url="/app/")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve a favicon so browsers stop 404ing /favicon.ico (log noise).

    Reuse the Flutter app's favicon.png if present, else answer 204 No Content
    so the browser stops asking without logging an error.
    """
    from fastapi.responses import Response

    icon = Path(__file__).parent / "static" / "app" / "favicon.png"
    if icon.exists():
        return FileResponse(str(icon), media_type="image/png")
    return Response(status_code=204)


@app.get("/media/file")
async def media_file(path: str, node: str = ".158"):
    """Stream a file from an exposed root for the skos media viewer.

    Same-origin + HTTP range-capable (via Starlette FileResponse), so images
    load efficiently and video/audio stream + seek without base64 or the 8 MiB
    /tool cap — this is what makes the 300 MB AI-LIFE masters viewable.
    Path safety reuses the access plane's allowlist + hard-deny secret checks.
    Local node (.158) only for now; remote nodes are a follow-up.
    """
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    try:
        from skcomms.access.files import get_default_access

        resolved = get_default_access()._resolve_checked(path, must_exist=True)
    except Exception as exc:  # PathDenied / traversal / hard-denied secret
        raise HTTPException(status_code=403, detail=f"denied: {exc}")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="not a regular file")
    return FileResponse(str(resolved))


# Per-node sk-access endpoints (for the same-origin /access/tool proxy below).
_ACCESS_NODES = {
    ".158": "http://100.108.59.57:9386",  # sk-access binds the tailnet IP, not loopback
    ".41": "http://100.86.156.5:9386",
}


@app.post("/access/tool")
async def access_tool_proxy(request: Request):
    """Same-origin proxy to a node's sk-access ``/tool``.

    Lets the web app reach the access plane over ANY origin (localhost, the
    https funnel, a cloudflared tunnel) with no mixed-content and no need for
    the browser to reach a tailnet IP directly. Every call still carries the
    capauth-signed token, so the access gate authorizes it exactly as before.
    Body: ``{node, token, tool, arguments}``.
    """
    import urllib.error
    import urllib.request

    from fastapi import HTTPException

    body = await request.json()
    node = body.get("node", ".158")
    base = _ACCESS_NODES.get(node, _ACCESS_NODES[".158"])
    payload = json.dumps(
        {k: body[k] for k in ("token", "tool", "arguments") if k in body}
    ).encode()
    req = urllib.request.Request(
        f"{base}/tool",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return JSONResponse(json.loads(r.read()))
    except urllib.error.HTTPError as e:
        try:
            return JSONResponse(json.loads(e.read() or b"{}"), status_code=e.code)
        except Exception:
            return JSONResponse({"detail": "access tool error"}, status_code=e.code)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"access node unreachable: {exc}")


#: Root-absolute URLs in proxied HTML: an ``href=`` / ``src=`` whose value begins
#: with a single ``/`` (a same-origin, root-absolute path). Captures the value up
#: to the closing quote. Matches BOTH asset loads (``/static/...``, ``/assets/...``)
#: AND in-app nav links (``/board``, ``/cockpit``, ``/models`` ...); protocol-
#: relative (``//host``) and already-prefixed values are filtered in the
#: substitution, so the rewrite stays correct and idempotent.
_HTML_ROOT_ABS_ATTR_RE = _re.compile(rb"""((?:href|src)\s*=\s*["'])(/[^"'\s>]*)""")


def _rewrite_html_asset_prefix(body: bytes, prefix: str, token: str = "") -> bytes:
    """Rewrite ROOT-ABSOLUTE ``href``/``src`` URLs in proxied HTML onto a
    same-origin module ``prefix`` (assets AND in-app nav), and (when ``token``
    is given) append ``?embed_token=...`` so each ``<link>``/``<script>``/nav
    load authorizes at the gated proxy WITHOUT a cookie.

    The embed iframe is sandboxed without ``allow-same-origin`` (A3 containment),
    so its document origin is OPAQUE and a path-scoped ``SameSite=Lax`` cookie is
    never attached to its subresource requests. The runtime shim already carries
    the token on ``fetch``/``XHR`` calls; this makes the STATIC ``href``/``src``
    subresource loads carry it too, closing the "styled-vs-unstyled dead HTML"
    gap where CSS/JS 401'd against the gate.

    A subapp (the skcapstone coordination dashboard) references BOTH its assets
    (``/static/css/board.css``, ``/static/js/cmdb.js``) and its own navigation
    (``<a href="/board">``, ``/cockpit``, ``/models`` ...) by ROOT-ABSOLUTE path.
    Served raw through the ``/skdashboard`` reverse-proxy prefix, the browser
    resolves those against the SHELL origin: asset loads hit the shell's own
    Flutter ``/static`` mount (blank pane), and a nav click NAVIGATES THE IFRAME
    OUT of the ``/skdashboard`` prefix into skchat's own routes (a dead pane).
    Rewriting every root-absolute ``/x`` -> ``/skdashboard/x`` keeps both the asset
    loads and the nav inside the proxied prefix.

    Left untouched (returned verbatim):

    * protocol-relative (``//cdn.example/x``) and absolute (``http(s)://...``)
      URLs, which are cross-origin and must not be reparented;
    * values already under ``prefix`` (``/skdashboard/...``), so the rewrite is
      IDEMPOTENT (no ``/skdashboard/skdashboard`` double-prefix).

    Applied ONLY to ``text/html`` bodies (dashboard CSS ``url()`` refs are all
    ``data:`` URIs, so stylesheets need no rewriting; ``.js`` runtime ``fetch``
    URLs are handled at dispatch time by :func:`_embed_fetch_shim`, not here)."""
    p = prefix.rstrip("/").encode()
    from urllib.parse import quote as _urlquote

    tok = _urlquote(token, safe="").encode() if token else b""

    def _add_token(url: bytes) -> bytes:
        # Idempotent: never double-append; pick ? or & for an existing query.
        if not tok or b"embed_token=" in url:
            return url
        sep = b"&" if b"?" in url else b"?"
        return url + sep + b"embed_token=" + tok

    def _sub(m: "_re.Match[bytes]") -> bytes:
        open_attr, value = m.group(1), m.group(2)
        # Protocol-relative (//host/...) -> cross-origin, never reparent/token.
        if value.startswith(b"//"):
            return m.group(0)
        # Already under the prefix -> keep the prefix (idempotent), but still
        # ensure the token so the gated subresource load authorizes.
        if value == p or value.startswith(p + b"/"):
            return open_attr + _add_token(value)
        return open_attr + _add_token(p + value)

    return _HTML_ROOT_ABS_ATTR_RE.sub(_sub, body)


#: Insert-point for the embed shim: right after the opening ``<head>`` tag
#: (case-insensitive). Pages with no ``<head>`` fall back to a top-of-body prepend.
_HTML_HEAD_OPEN_RE = _re.compile(rb"(<head[^>]*>)", _re.IGNORECASE)


def _embed_fetch_shim(prefix: str, token: str) -> bytes:
    """A tiny classic ``<script>`` that reparents the proxied subapp's RUNTIME
    root-absolute ``fetch``/``XHR`` requests onto ``prefix`` and (when present)
    attaches the read-only ``embed_token``.

    Why a runtime shim and not a static rewrite: the dashboard's data calls live in
    ES-module ``.js`` files (``api.js``: ``fetch('/api/card/...')``) served through
    the proxy as ``application/javascript``. Regex-rewriting URLs inside JS is
    fragile (template literals, strings, comments); wrapping ``window.fetch`` /
    ``XMLHttpRequest.open`` catches every call at dispatch time -- including
    template-literal URLs -- without editing the subapp's source.

    Why the token rides the URL (not a cookie): the embed iframe is sandboxed
    WITHOUT ``allow-same-origin`` (A3 containment), so its document origin is
    OPAQUE. A ``fetch`` from an opaque origin to the funnel is CROSS-ORIGIN, so the
    default ``credentials:'same-origin'`` mode sends NO cookies -- the path-scoped
    embed cookie (which authorizes the ``<link>``/``<script>`` asset loads fine)
    never reaches ``fetch``. Carrying the token as a query param authorizes the
    request without a cookie, exactly the skcode token-in-URL model; the module
    proxy adds ``Access-Control-Allow-Origin: *`` so the opaque-origin pane can READ
    the reply.

    Only ROOT-ABSOLUTE (``/x``; not ``//host``, not already ``prefix``) URLs are
    touched, so cross-origin and already-prefixed calls (e.g. the prefix-aware
    ``models.html``) stay correct."""
    p = json.dumps(prefix.rstrip("/"))
    t = json.dumps(token or "")
    js = (
        '(function(){"use strict";'
        "var P=%s,T=%s;"
        'function internal(u){return typeof u==="string"&&u.charAt(0)==="/"&&u.charAt(1)!=="/";}'
        'function withPrefix(u){return (u===P||u.indexOf(P+"/")===0)?u:P+u;}'
        'function withToken(u){if(!T||u.indexOf("embed_token=")!==-1)return u;'
        'return u+(u.indexOf("?")===-1?"?":"&")+"embed_token="+encodeURIComponent(T);}'
        "function fix(u){return internal(u)?withToken(withPrefix(u)):u;}"
        "var of=window.fetch;"
        "if(of){window.fetch=function(input,init){try{"
        'if(typeof input==="string"){input=fix(input);}'
        'else if(input&&typeof input.url==="string"){input=new Request(fix(input.url),input);}'
        "}catch(e){}return of.call(this,input,init);};}"
        "var xo=XMLHttpRequest.prototype.open;"
        "XMLHttpRequest.prototype.open=function(){try{"
        'if(typeof arguments[1]==="string"){arguments[1]=fix(arguments[1]);}'
        "}catch(e){}return xo.apply(this,arguments);};"
        # EventSource (SSE) is a THIRD dispatch path, like ES-module imports: it
        # neither goes through window.fetch/XHR nor can carry a header, so its
        # root-absolute URL (the dashboard's /api/events live stream) must be
        # prefixed + tokened here too, or it 404s off the /skdashboard prefix.
        "var OE=window.EventSource;"
        "if(OE){var NE=function(u,c){try{if(typeof u===\"string\"){u=fix(u);}}catch(e){}return new OE(u,c);};"
        "NE.prototype=OE.prototype;try{NE.CONNECTING=OE.CONNECTING;NE.OPEN=OE.OPEN;NE.CLOSED=OE.CLOSED;}catch(e){}"
        "window.EventSource=NE;}"
        "})();"
    ) % (p, t)
    return b"<script>" + js.encode("utf-8") + b"</script>"


#: Marker so re-proxying the same body never double-injects the shim.
_EMBED_SHIM_MARKER = b"<!--SKEMBED_SHIM-->"


def _inject_embed_shim(body: bytes, prefix: str, token: str) -> bytes:
    """Inject :func:`_embed_fetch_shim` as the FIRST child of ``<head>``.

    A classic inline script runs before the deferred ES-module scripts, so
    ``window.fetch`` is patched before the dashboard issues its first call. Falls
    back to a top prepend when the page has no ``<head>``. Idempotent: a body that
    already carries the shim marker is returned unchanged."""
    if _EMBED_SHIM_MARKER in body:
        return body
    shim = _EMBED_SHIM_MARKER + _embed_fetch_shim(prefix, token)
    m = _HTML_HEAD_OPEN_RE.search(body)
    if m:
        i = m.end()
        return body[:i] + shim + body[i:]
    return shim + body


async def _reverse_proxy(
    request: Request,
    upstream: str,
    path: str,
    *,
    label: str,
    html_prefix: str | None = None,
    embed_token: str | None = None,
):
    """Raw same-origin reverse proxy to a sibling subapp daemon (``upstream``),
    so the SKWorld shell's pane reaches it over the 443 funnel like every other
    call, never a direct daemon port. Preserves method/body/status/content-type
    so the subapp's web client + assets load; adds NO auth of its own (each
    subapp keeps its own gate). Upstream is overridable per node via env.

    When ``html_prefix`` is set, ``text/html`` response bodies are transformed so an
    embedded subapp stays inside the proxy prefix and can reach its API:

    * root-absolute ``href``/``src`` (assets AND nav) are rewritten onto the prefix
      (see :func:`_rewrite_html_asset_prefix`); and
    * a runtime ``fetch``/``XHR`` shim is injected (see :func:`_embed_fetch_shim`)
      that reparents root-absolute API calls onto the prefix and, when
      ``embed_token`` is given, attaches it as a query param so the opaque-origin
      iframe's cross-origin data calls authorize without a cookie."""
    import urllib.error
    import urllib.request

    from fastapi.responses import Response

    url = f"{upstream.rstrip('/')}/{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    body = await request.body()
    fwd = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "connection")
    }
    req = urllib.request.Request(
        url,
        data=body if request.method == "POST" else None,
        method=request.method,
        headers=fwd,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            ctype = r.headers.get("content-type", "application/octet-stream")
            data = r.read()
            if html_prefix and "text/html" in ctype.lower():
                data = _rewrite_html_asset_prefix(data, html_prefix, embed_token or "")
                data = _inject_embed_shim(data, html_prefix, embed_token or "")
            return Response(
                content=data,
                status_code=r.status,
                media_type=ctype,
            )
    except urllib.error.HTTPError as e:
        return Response(
            content=e.read() or b"",
            status_code=e.code,
            media_type=e.headers.get("content-type", "text/plain"),
        )
    except Exception as exc:
        return Response(
            content=f"{label} unavailable: {exc}".encode(),
            status_code=502,
            media_type="text/plain",
        )


def _authorize_module_proxy(request: Request, module: str) -> str:
    """Authorize a gated module proxy request. Returns how it was authorized.

    Two accepted credentials, in order:

    * A valid ``Authorization`` operator/dataplane credential -> ``"auth"``: FULL
      access (read + write), exactly as a direct authenticated call.
    * A valid module-scoped ``embed_token`` (query param or the path-scoped
      cookie) -> ``"embed"``. The iframe pane cannot set a header, so the
      authenticated app mints this short-lived, module-scoped token and hangs it
      off the iframe ``src``. Write authority follows the token's ``mode``:
        - ``ro`` (read-only): a non-GET/HEAD request is refused (403), so a
          read-only pane can never mutate.
        - ``rw`` (read + write): non-GET/HEAD (POST/PUT/DELETE) is allowed, so the
          trusted first-party admin pane (skdashboard) re-enables its in-pane Save
          actions for the same already-authenticated operator. ``rw`` tokens are
          only minted for a trusted-module allowlist presented by a full operator
          credential (see :func:`embed_token_mint`).

    When neither is present the request falls through to
    :func:`enforce_dataplane_auth`, which raises 401 when the plane-wide gate is
    on (leak stays closed). skcode is intentionally NOT routed through here: it
    runs its own deny-all gate and its public client shell is safe to expose.
    """
    # Full operator/dataplane credential: read + write. Consulted independently of
    # the plane-wide flag (like the audience-token mint), so a valid Bearer works.
    if request_is_authenticated(request):
        return "auth"
    # Module-scoped embed token: read for any mode; write only for rw.
    et = request_embed_token(request, module)
    if et is not None:
        if request.method not in ("GET", "HEAD") and not et.writable:
            raise HTTPException(status_code=403, detail="embed token is read-only")
        return "embed"
    # Neither: apply the plane-wide gate (401 when SKCHAT_DATAPLANE_AUTH is on).
    enforce_dataplane_auth(request)
    return "gate"


def _set_embed_cookie(response, request: Request, module: str) -> None:
    """Hand a path-scoped embed cookie to the pane on its first navigation.

    Only when the token arrived as a query param (the initial iframe ``src`` load):
    the pane's subsequent subresource requests (its own ``fetch`` / asset loads)
    cannot re-attach the query param, so the cookie carries the SAME token for the
    rest of its short life. Scoped to the module's proxy ``Path``, HttpOnly, and
    ``Secure`` on https so it never leaks to another module or to script.
    """
    token = (request.query_params.get("embed_token") or "").strip()
    if not token:
        return
    try:
        from .embed_auth import verify_embed_token

        et = verify_embed_token(token, module)
    except EmbedAuthError:
        return
    max_age = max(1, et.exp - int(datetime.now(timezone.utc).timestamp()))
    response.set_cookie(
        key=cookie_name(module),
        value=token,
        max_age=max_age,
        path=cookie_path(module),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
    )


#: Default upstream for skcode-hostd (tailnet-only bind, port 9394). Overridable
#: per node via SKCODE_HOSTD_URL. Kept in one place so the GET/POST proxy and the
#: WebSocket tail proxy below always target the same host.
DEFAULT_SKCODE_HOSTD_URL = "http://100.108.59.57:9394"


def _skcode_upstream() -> str:
    """The skcode-hostd base URL this webui proxies to (env-overridable)."""
    return os.environ.get("SKCODE_HOSTD_URL", DEFAULT_SKCODE_HOSTD_URL).rstrip("/")


def _skcode_ws_url(upstream: str, path: str, query: str) -> str:
    """Map an http(s) upstream base + proxied path (+ query) to the ws(s) URL of
    the same route on skcode-hostd. The browser connects to
    ``wss://<origin>/skcode/api/v1/sessions/<sid>/stream?token=...``; this rebuilds
    the equivalent ``ws://<host>:9394/api/v1/sessions/<sid>/stream?token=...`` on
    the tailnet-only host. The token rides the query string verbatim (browsers
    cannot set headers on a WebSocket), so the host's own gate still decides."""
    base = upstream.rstrip("/")
    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        ws_base = "ws://" + base[len("http://") :]
    else:
        ws_base = base
    url = f"{ws_base}/{path.lstrip('/')}"
    if query:
        url = f"{url}?{query}"
    return url


#: CORS for the opaque-origin embed iframe (A3 containment). The skcode client
#: runs in a sandboxed null origin (no allow-same-origin), so its authenticated
#: /skcode/api calls are cross-origin. These headers let the sandboxed client
#: carry its passed-in skcode token (Bearer) and READ the response. The token is
#: still the only gate (hostd verifies audience + scope + signature); CORS never
#: bypasses that, it only lets the browser surface the response to the confined
#: client. Non-credentialed (Bearer, not cookies), so "*" is valid + safe here.
_SKCODE_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Max-Age": "600",
}


#: Methods the gated module proxies expose. Writes (POST/PUT/DELETE) are only
#: honored for a full operator credential OR a mode=rw embed token (see
#: :func:`_authorize_module_proxy`); OPTIONS is the CORS preflight, served
#: unauthenticated (browsers send no credentials on a preflight).
_MODULE_PROXY_METHODS = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]


def _module_cors_headers(request: Request) -> dict:
    """CORS headers for a gated module proxy (opaque-origin embed iframe).

    The embed iframe is sandboxed WITHOUT ``allow-same-origin`` (A3 containment),
    so its document origin is OPAQUE and its in-pane ``fetch`` calls are
    cross-origin. A JSON-content-type write (the Models console Save posts
    ``application/json``) triggers a CORS PREFLIGHT ``OPTIONS`` the browser sends
    with NO credentials; these headers let that preflight (and the subsequent
    write) pass. The embed token stays the ONLY gate: it rides the URL/query, not
    a cookie, so this is NON-CREDENTIALED CORS (no ``Allow-Credentials``) and the
    echoed/`*` origin never grants ambient authority. ``Allow-Headers`` includes
    ``Content-Type`` so the JSON body's content-type header is permitted.
    """
    origin = request.headers.get("origin") or "*"
    return {
        # Echo the caller's origin (an opaque-origin iframe sends ``null``); fall
        # back to ``*`` when no Origin header is present. Non-credentialed, so this
        # is safe and never confers cookie-backed authority.
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
        "Access-Control-Max-Age": "600",
        "Vary": "Origin",
    }


@app.api_route("/skcode/{path:path}", methods=["GET", "POST", "OPTIONS"])
async def skcode_proxy(path: str, request: Request):
    """/skcode/* -> skcode-hostd (tailnet-only, deny-all; SKCODE_HOSTD_URL,
    default :9394). The public client shell proxies through; /api/v1 stays 401
    until this device presents a valid audience=skcode token."""
    if request.method == "OPTIONS":
        from starlette.responses import Response as _Resp

        return _Resp(status_code=204, headers=_SKCODE_CORS)
    resp = await _reverse_proxy(request, _skcode_upstream(), path, label="skcode host")
    for _k, _v in _SKCODE_CORS.items():
        resp.headers[_k] = _v
    # Never cache the skcode client/app or its API. Mobile browsers heuristically
    # cache HTML with no cache headers, which pins a device to a STALE client
    # (an old inject path that posts empty session ids, etc.). no-store forces a
    # fresh client + fresh token on every load.
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.websocket("/skcode/{path:path}")
async def skcode_ws_proxy(websocket: WebSocket, path: str) -> None:
    """Same-origin WebSocket proxy for skcode-hostd's session tail
    (``/api/v1/sessions/{sid}/stream``), so live session output reaches the shell
    over the 443 funnel exactly like the GET/POST proxy above, never a direct
    daemon port.

    AUTH is UNCHANGED: skcode-hostd runs its own deny-all gate and reads the wire
    token from the WS query string (browsers cannot set a WebSocket Authorization
    header). This proxy forwards that query string verbatim and adds no auth of its
    own, so the host still fails closed. When the host refuses the token it closes
    the handshake, and we relay that to the browser as a 1008 close (which the
    read-only client renders as its "pair this device" hint), never a silent hang.
    """
    import websockets
    import websockets.exceptions as _ws_exc

    # Handshake-rejection class differs across websockets versions (InvalidStatus
    # in v14+, InvalidStatusCode earlier); accept whichever this build exposes.
    _reject_errors = tuple(
        e
        for e in (
            getattr(_ws_exc, "InvalidStatus", None),
            getattr(_ws_exc, "InvalidStatusCode", None),
        )
        if e is not None
    ) or (Exception,)

    upstream_url = _skcode_ws_url(_skcode_upstream(), path, websocket.url.query)
    await websocket.accept()

    try:
        upstream = await websockets.connect(upstream_url, open_timeout=10)
    except _reject_errors:
        # The host rejected the handshake (deny-all / bad token). Mirror its
        # policy close so the client shows the pairing hint, not an error.
        await websocket.close(code=1008)
        return
    except Exception as exc:  # noqa: BLE001 - host unreachable / network error.
        logger.warning("skcode ws proxy connect failed: %s", exc)
        await websocket.close(code=1011)
        return

    async def _client_to_upstream() -> None:
        # The read-only tail client never sends, but pump anyway so the bridge is
        # correct for any future duplex route.
        try:
            while True:
                msg = await websocket.receive_text()
                await upstream.send(msg)
        except WebSocketDisconnect:
            return
        except Exception:  # noqa: BLE001
            return

    async def _upstream_to_client() -> None:
        try:
            async for msg in upstream:
                if isinstance(msg, bytes):
                    await websocket.send_bytes(msg)
                else:
                    await websocket.send_text(msg)
        except Exception:  # noqa: BLE001 - upstream closed / errored.
            return

    up = asyncio.create_task(_upstream_to_client())
    down = asyncio.create_task(_client_to_upstream())
    try:
        # When either side closes, tear down both.
        done, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
    finally:
        await upstream.close()
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 - already closed.
            pass


@app.api_route("/skdashboard/{path:path}", methods=_MODULE_PROXY_METHODS)
async def skdashboard_proxy(path: str, request: Request):
    """/skdashboard/* -> the skcapstone coordination dashboard (SKDASHBOARD_URL,
    default :7778) so the shell's "Board" pane loads over the 443 funnel.

    GATED: unlike skcode (which runs its own deny-all gate), the coord dashboard
    has NO auth of its own, so proxying it unauthenticated over the PUBLIC funnel
    would expose the whole coordination board (task list, agent status). It
    accepts EITHER a full operator credential (read + write) OR a short-lived,
    module-scoped ``embed_token`` the authenticated app mints for the iframe pane
    (see ``embed_auth``). A ``ro`` token authorizes reads only; a ``rw`` token
    (minted only for this trusted first-party admin module, only for an operator
    credential) additionally authorizes writes (the Models console Save, cmdb
    seed, kanban mutations). An unauth request with no/invalid token still 401s,
    so the leak stays closed.

    OPTIONS is served here as the CORS preflight (unauthenticated): the pane's
    in-pane JSON-content-type write is cross-origin (opaque iframe origin) and the
    browser preflights it, so the proxy must answer the preflight before the write
    ever carries its token."""
    if request.method == "OPTIONS":
        from starlette.responses import Response as _Resp

        return _Resp(status_code=204, headers=_module_cors_headers(request))
    # Public UI assets (``static/*``: js/css/fonts) are dashboard CODE, not data.
    # The dashboard's pages are ES modules whose relative imports
    # (``import ... from "./api.js"``) resolve WITHOUT the navigation's query
    # string, so they cannot carry the ``embed_token``; gating them 401s
    # ``api.js`` and every page's modules under the embed (the pane just spins
    # and JS-rendered UI dies) while a direct :7778 hit works. Exempt read-only
    # ``static/*`` (traversal-guarded) from the token: the DATA endpoints
    # (``/skdashboard/api/*``) and the page routes stay gated, and the injected
    # fetch shim still tokens the runtime API calls. Never exempt a mutation.
    _is_static_asset = (
        request.method in ("GET", "HEAD")
        and path.startswith("static/")
        and ".." not in path
    )
    how = "static" if _is_static_asset else _authorize_module_proxy(request, "skdashboard")
    upstream = os.environ.get("SKDASHBOARD_URL", "http://127.0.0.1:7778")
    # The token the injected fetch/XHR shim will hang off in-pane API calls: from
    # the initial navigation's query param, else the path-scoped cookie set on that
    # first load (which the HttpOnly cookie hides from the pane's own JS, so the
    # proxy must read it server-side and inject it).
    embed_tok = (
        request.query_params.get("embed_token")
        or request.cookies.get(cookie_name("skdashboard"))
        or ""
    ).strip()
    resp = await _reverse_proxy(
        request,
        upstream,
        path,
        label="skdashboard",
        html_prefix="/skdashboard",
        embed_token=embed_tok,
    )
    # The embed iframe is opaque-origin (A3, no allow-same-origin), so its in-pane
    # fetch()es to this proxy are CROSS-ORIGIN and unreadable without CORS. Allow
    # the read (and, for a rw token, the write response): non-credentialed (the
    # token rides the URL, not a cookie), so echoing the origin / "*" is valid +
    # safe, the same model as the skcode pane. The embed token stays the only gate;
    # CORS never bypasses _authorize_module_proxy.
    for _k, _v in _module_cors_headers(request).items():
        resp.headers[_k] = _v
    if how == "embed" and presented_via_query(request):
        _set_embed_cookie(resp, request, "skdashboard")
    return resp


@app.api_route("/skos/{path:path}", methods=_MODULE_PROXY_METHODS)
async def skos_proxy(path: str, request: Request):
    """/skos/* -> the skos read-only web surface (SKOS_URL, default :7781) so the
    shell's "OS" pane loads over the 443 funnel. GATED for the same reason as
    skdashboard: skos's surface has no auth of its own, so it must not be public
    over the funnel. Accepts a full operator credential OR a module-scoped,
    read-only ``embed_token`` (see ``embed_auth``); otherwise 401.

    Gets the SAME embed treatment as skdashboard (previously missing, so the pane
    rendered as bare unstyled HTML): the html_prefix + token rewrite/shim keep its
    assets/nav/API inside the /skos prefix and authorized, and the CORS headers let
    the opaque-origin iframe READ the proxied replies."""
    if request.method == "OPTIONS":
        from starlette.responses import Response as _Resp

        return _Resp(status_code=204, headers=_module_cors_headers(request))
    how = _authorize_module_proxy(request, "skos")
    upstream = os.environ.get("SKOS_URL", "http://127.0.0.1:7781")
    embed_tok = (
        request.query_params.get("embed_token") or request.cookies.get(cookie_name("skos")) or ""
    ).strip()
    resp = await _reverse_proxy(
        request,
        upstream,
        path,
        label="skos",
        html_prefix="/skos",
        embed_token=embed_tok,
    )
    for _k, _v in _module_cors_headers(request).items():
        resp.headers[_k] = _v
    if how == "embed" and presented_via_query(request):
        _set_embed_cookie(resp, request, "skos")
    return resp


#: Env allowlist of modules for which a mode=rw embed token may be minted.
#: Comma/space-separated; defaults to the single trusted first-party admin module
#: ``skdashboard``. A module NOT on this list can only ever get a ro token, so an
#: embedded write surface is opt-in per module and can be narrowed to none by
#: setting the var empty. Read at call time so an operator editing the unit's
#: ``Environment=`` line (or a test) can change it without a reimport.
_EMBED_RW_MODULES_ENV = "SKCHAT_EMBED_RW_MODULES"
_DEFAULT_EMBED_RW_MODULES = "skdashboard"


def _embed_rw_modules() -> frozenset:
    """The set of modules for which a rw embed token may be minted (env-driven)."""
    raw = os.environ.get(_EMBED_RW_MODULES_ENV, _DEFAULT_EMBED_RW_MODULES)
    parts = [p.strip() for p in raw.replace(",", " ").split()]
    # Only ever allow rw for a module that is also a real embeddable gated module.
    return frozenset(p for p in parts if p and p in EMBED_MODULES)


@app.post("/api/v1/embed-token")
async def embed_token_mint(request: Request) -> JSONResponse:
    """Mint a short-lived, module-scoped embed token for an iframe pane.

    The shell's Grade B panes (``/skdashboard``, ``/skos``) are iframes that cannot
    set an ``Authorization`` header, so once those proxies are gated they can only
    401. The AUTHENTICATED app calls this to obtain a token it appends to the iframe
    ``src`` as ``?embed_token=...``; the proxy then accepts that token (scoped to
    the exact module) for the token's short life.

    Two gates, both required (mirrors the audience-token mint):

    * GATE 1 (flag): ``SKCHAT_EMBED_TOKENS`` (default OFF). When off the route is
      INERT (404, never mints), so the app is byte-identical to before it existed.
    * GATE 2 (auth): the request MUST carry a valid operator/capauth credential
      (validated via :func:`request_is_authenticated`). An unauthenticated caller
      gets 401 and no token is ever minted, even with the flag on. So a token can
      only come into existence via an authenticated request. NOTE: an embed token
      is NOT an operator credential (it rides the URL/query, not a validated
      dataplane Bearer), so it can never satisfy this gate and thus can never be
      used to mint another (let alone a rw) token.

    Body (JSON): ``{"module": "skdashboard" | "skos", "mode": "ro" | "rw"}``. The
    module MUST be one of the gated proxy modules; any other value is 400. ``mode``
    defaults to ``ro``. A ``rw`` request is honored ONLY for a module on the
    ``SKCHAT_EMBED_RW_MODULES`` allowlist (default ``skdashboard``); requesting
    ``rw`` for any other module is 403. This re-enables the embedded write surface
    (Models console Save etc.) for the trusted first-party admin pane and the same
    already-authenticated operator, without granting a new privilege.
    """
    # GATE 1: flag. Inert (looks like the route does not exist) when off.
    if not embed_tokens_enabled():
        raise HTTPException(status_code=404, detail="not found")

    # GATE 2: PRIMARY authentication. Always enforced when minting, independent of
    # the plane-wide gate flag, so we never mint for an anonymous caller. CR-3.4 P3:
    # a PRIMARY operator credential (operator session or FQID assertion) is required;
    # neither an embed token nor an audience token can satisfy this (no laundering).
    if not request_is_primary_authenticated(request):
        raise HTTPException(status_code=401, detail="capauth authentication required")

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    module = body.get("module")
    if not isinstance(module, str) or module not in EMBED_MODULES:
        raise HTTPException(
            status_code=400,
            detail=f"module must be one of {sorted(EMBED_MODULES)}",
        )

    # Requested mode (default ro). Only the ro/rw values are legitimate; anything
    # else is a bad request rather than a silent downgrade.
    mode = body.get("mode", MODE_RO)
    if mode not in (MODE_RO, MODE_RW):
        raise HTTPException(status_code=400, detail=f"mode must be one of {[MODE_RO, MODE_RW]}")
    # rw is only mintable for the trusted-module allowlist. Deny (403) for any
    # other module so an embedded write surface stays scoped to skdashboard.
    if mode == MODE_RW and module not in _embed_rw_modules():
        raise HTTPException(
            status_code=403,
            detail=f"module {module!r} may not be granted a read-write embed token",
        )

    try:
        token, exp = mint_embed_token(module, mode=mode)
    except EmbedAuthError as exc:  # missing signing key etc: fail closed, no token.
        logger.warning("embed-token mint failed: %s", exc)
        raise HTTPException(status_code=500, detail="embed token mint failed")

    logger.info("embed-token minted module=%s mode=%s exp=%s", module, mode, exp)
    return JSONResponse(
        {
            "token": token,
            "module": module,
            "mode": mode,
            "expires_at": datetime.fromtimestamp(exp, tz=timezone.utc).isoformat(),
        }
    )


@app.get("/.well-known/skworld-module.json")
async def skworld_module_manifest(request: Request) -> JSONResponse:
    """skchat's SKWorld module manifest (public discovery metadata, no bearer).

    The shell reads this to learn skchat's entry, nav, and required auth
    audience/scopes before it has a token. URLs are origin-relative to the request.
    """
    from .skworld_manifest import skchat_module_manifest

    return JSONResponse(skchat_module_manifest(str(request.base_url)))


@app.get("/api/v1/shell/modules")
async def shell_modules(request: Request) -> JSONResponse:
    """Aggregate every discoverable SKWorld subapp manifest for the shell.

    Public discovery metadata (no bearer, no dataplane auth, like the
    /.well-known/skworld-module.json route): ONE same-origin endpoint, reachable
    over the 443 funnel, so the shell learns about all subapps at once instead of
    probing each daemon port. Returns ``{"modules": [<skworld.module.json>, ...]}``
    aggregating skchat's own manifest, skcode's, skdashboard's, and every
    statically-emitted ``*.skworld-module.json`` in the node's shell registry dir.
    Best-effort: any unreachable/missing source is skipped, never fails the whole
    response. See ``shell_modules.aggregate_shell_modules`` and umbrella shell
    design 5.3 (Registry).
    """
    from .shell_modules import aggregate_shell_modules

    return JSONResponse({"modules": aggregate_shell_modules(str(request.base_url))})


@app.post("/api/v1/audience-token")
async def audience_token_mint(request: Request) -> JSONResponse:
    """Mint a fresh audience-scoped capauth token for THIS daemon's own identity.

    Closes the shell->token gap: the Flutter shell's ``AuthContext.token()`` is
    stubbed because there was no backend to mint from. The shell calls this to get
    a real, short-lived (default 1h) token scoped to the ``skchat`` audience, in
    the exact wire form skchat's own dataplane accepts (base64url of
    ``capauth.export_token``), which the shell then presents on data-plane calls.

    Two gates, both required:

    * GATE 1 (flag): ``SKCHAT_AUDIENCE_MINT`` (read at call time), default OFF. When
      off the route is INERT (404, never mints), so the app is byte-identical to
      before this endpoint existed.
    * GATE 2 (auth): the request MUST carry a valid capauth credential
      (operator-session JWT or signed FQID assertion), validated via
      :func:`request_is_authenticated`. An unauthenticated caller gets 401 and no
      token is ever minted, even with the flag on.

    Body (optional JSON): ``{"audience": "skchat", "scopes": [...]}``. ``audience``
    defaults to ``skchat``; ``scopes`` defaults to the audience's standard scope set.

    Anti-forgery: the token SUBJECT is resolved server-side from
    :func:`capauth.resolve_agent_identity` (this daemon's own identity). No subject
    or agent is ever read from request input, so an authenticated caller cannot mint
    a token for any identity other than the daemon it is talking to. Fails closed
    (500, no token, logged) on any mint error.
    """
    # GATE 1: flag. Inert (looks like the route does not exist) when off.
    if not audience_mint_enabled():
        raise HTTPException(status_code=404, detail="not found")

    # GATE 2: PRIMARY authentication. Always enforced when minting, independent of
    # the plane-wide SKCHAT_DATAPLANE_AUTH gate, so we never mint for an anon caller.
    # CR-3.4 P3: the credential must be PRIMARY (operator session or FQID assertion);
    # an audience token can never mint another (no renewal laundering).
    if not request_is_primary_authenticated(request):
        raise HTTPException(status_code=401, detail="capauth authentication required")

    # Parse optional body: audience + scopes only. Never a subject/agent.
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    audience = body.get("audience") or "skchat"
    if not isinstance(audience, str):
        raise HTTPException(status_code=400, detail="audience must be a string")
    scopes = body.get("scopes")
    if scopes is not None and not isinstance(scopes, list):
        raise HTTPException(status_code=400, detail="scopes must be a list")

    try:
        from capauth import (
            export_token,
            mint_agent_audience_token,
            resolve_agent_identity,
        )

        # Anchor the subject to THIS daemon's resolved identity (never request
        # input). mint_agent_audience_token(agent=None) resolves the same active
        # identity internally; resolving here first surfaces identity errors and
        # documents that the subject is server-derived, not caller-supplied.
        identity = resolve_agent_identity()
        token = mint_agent_audience_token(agent=None, audience=audience, scopes=scopes)
        import base64

        wire = (
            base64.urlsafe_b64encode(export_token(token).encode("utf-8"))
            .decode("ascii")
            .rstrip("=")
        )
    except Exception as exc:  # fail closed: no token leaves on any mint error.
        logger.warning("audience-token mint failed: %s", exc)
        raise HTTPException(status_code=500, detail="audience token mint failed")

    expires_at = token.payload.expires_at
    logger.info(
        "audience-token minted subject=%s audience=%s",
        getattr(identity, "fqid", None) or getattr(identity, "uri", None),
        token.payload.audience,
    )
    return JSONResponse(
        {
            "token": wire,
            "audience": token.payload.audience,
            "expires_at": expires_at.isoformat()
            if hasattr(expires_at, "isoformat")
            else expires_at,
        }
    )


@app.get("/health")
async def health() -> JSONResponse:
    """Health check endpoint for container orchestration.

    Includes the resolved agent name and current OOF level so a swarm
    healthcheck or external monitor can spot identity-drift or stuck-FEB
    bugs without scraping a separate endpoint.
    """
    try:
        from .agent_profile import get_active_agent_name, load_feb_state

        agent = get_active_agent_name()
        feb = load_feb_state(agent)
        return JSONResponse(
            {
                "status": "ok",
                "service": "skchat-webui",
                "version": __version__,
                "agent": agent,
                "oof_level": feb.oof_level,
                "has_feb": feb.has_feb,
            }
        )
    except Exception as e:
        logger.warning("webui.py: %s", e)
        return JSONResponse({"status": "ok", "service": "skchat-webui", "version": __version__})


@app.get("/agent/state")
async def agent_state() -> JSONResponse:
    """Return the running agent's identity, soul summary, and FEB state.

    This is the canonical "who am I and how do I feel" endpoint. The webui
    has no way to surface this without a real agent profile loader; before
    the v0.3.2 fix the page-rendered identity was hardcoded to
    ``capauth:skchat@skworld.io`` and OOF defaulted to 100% because no FEB
    selection ever ran. ``/agent/state`` is the diagnostic surface that
    proves both fixes landed.
    """
    try:
        from .agent_profile import load_agent_profile

        profile = load_agent_profile()
        return JSONResponse(profile.to_dict())
    except Exception as exc:
        logger.warning("webui.py: %s", exc)
        return JSONResponse(
            {"error": "agent_profile_load_failed", "detail": str(exc)},
            status_code=500,
        )


def _get_adapter_registry():
    """Locate the live AdapterRegistry, if one has been instantiated.

    The registry is the channel-adapter health source (skcomms transport
    adapters: matrix, telegram, p2p, …). It may not exist yet — the webui
    can boot long before any adapters are wired — so this resolver returns
    ``None`` gracefully rather than raising. Tests monkeypatch this to inject
    a stub registry.
    """
    try:
        from . import integration as _integration

        reg = getattr(_integration, "adapter_registry", None)
        if reg is not None:
            return reg
    except Exception as exc:  # pragma: no cover - defensive import guard
        logger.debug("adapter registry unavailable (%s: %s)", type(exc).__name__, exc)
    return None


def _adapter_health(adapter) -> dict:
    """Normalise one adapter into the documented health shape.

    Shape: ``{name, channel_type, connected, latency_ms, error}``. Every
    field is read defensively so a partially-initialised or duck-typed
    adapter never breaks the endpoint.
    """

    def _attr(obj, *names, default=None):
        for n in names:
            if hasattr(obj, n):
                return getattr(obj, n)
        return default

    err = _attr(adapter, "error", "last_error", default=None)
    return {
        "name": _attr(adapter, "name", default=None),
        "channel_type": _attr(adapter, "channel_type", "channel", "type", default=None),
        "connected": bool(_attr(adapter, "connected", "is_connected", default=False)),
        "latency_ms": _attr(adapter, "latency_ms", "latency", default=None),
        "error": str(err) if err is not None else None,
    }


@app.get("/adapters")
async def adapters() -> JSONResponse:
    """Report channel-adapter health.

    Returns a JSON list of ``{name, channel_type, connected, latency_ms,
    error}`` read from the live ``AdapterRegistry``. When no registry has
    been instantiated this returns an empty list with a 200 — it must never
    500 just because adapters aren't wired up yet.
    """
    registry = _get_adapter_registry()
    if registry is None:
        return JSONResponse([])

    try:
        # Support a few common registry surfaces: an ``adapters()`` method, an
        # ``adapters`` attribute (list or dict), or a plain iterable.
        adapters_obj = getattr(registry, "adapters", None)
        if callable(adapters_obj):
            items = adapters_obj()
        elif adapters_obj is not None:
            items = adapters_obj
        else:
            items = registry

        if isinstance(items, dict):
            items = items.values()

        return JSONResponse([_adapter_health(a) for a in items])
    except Exception as exc:
        logger.warning("webui.py /adapters: %s", exc)
        return JSONResponse([])


# Serve /voice page even when torch/silero are unavailable (voice WS won't work
# but the static HTML page will still load and attempt to connect)
if not _voice_routes_loaded:
    from fastapi.responses import FileResponse as _FileResponse

    @app.get("/voice", response_class=HTMLResponse)
    async def voice_chat_page_fallback() -> HTMLResponse:
        _static = Path(__file__).parent / "static" / "voice-chat.html"
        if _static.exists():
            return _FileResponse(_static, media_type="text/html")
        return HTMLResponse("<h1>voice-chat.html not found</h1>", status_code=404)


# ── WebSocket connection registry ─────────────────────────────────────────────

_ws_connections: set[WebSocket] = set()
_last_push_dt: Optional[datetime] = None


async def _ws_broadcast(msg_dict: dict) -> None:
    """Send a JSON payload to all connected WebSocket clients."""
    if not _ws_connections:
        return
    payload = json.dumps(msg_dict, default=str)
    dead: list[WebSocket] = []
    for ws in list(_ws_connections):
        try:
            await ws.send_text(payload)
        except Exception as e:
            logger.warning("webui.py: %s", e)
            dead.append(ws)
    for ws in dead:
        _ws_connections.discard(ws)


# ── helpers ───────────────────────────────────────────────────────────────────


def _get_identity() -> str:
    """Resolve the running agent's CapAuth identity URI.

    Order:
        1. Active SK agent profile (``SKAGENT`` / ``SKCAPSTONE_AGENT``) →
           per-agent ``identity/identity.json`` or convention
           ``capauth:{agent}@skworld.io``. This is the sovereign path —
           when the operator launches as ``SKAGENT=lumina``, the webui
           identifies as Lumina, not as the literal "skchat" service.
        2. ``identity_bridge.get_sovereign_identity()`` for legacy
           single-identity deployments.
        3. ``SKCHAT_IDENTITY`` env var (the historical hardcoded shim).
        4. ``~/.skchat/config.yml`` ``skchat.identity.uri``.
        5. ``capauth:local@skchat`` floor.
    """
    try:
        from .agent_profile import get_active_agent_name, get_agent_identity

        if get_active_agent_name() is not None:
            return get_agent_identity()
    except Exception as e:
        logger.warning("webui.py: %s", e)
        pass

    try:
        from .identity_bridge import get_sovereign_identity

        return get_sovereign_identity()
    except Exception as e:
        logger.warning("webui.py: %s", e)
        pass
    identity = os.environ.get("SKCHAT_IDENTITY")
    if identity:
        return identity
    config = _SKCHAT_HOME / "config.yml"
    if config.exists():
        try:
            import yaml

            with open(config) as f:
                cfg = yaml.safe_load(f)
            return cfg.get("skchat", {}).get("identity", {}).get("uri", "capauth:local@skchat")
        except Exception as e:
            logger.warning("webui.py: %s", e)
            pass
    return "capauth:local@skchat"


def _get_history():
    from .history import ChatHistory

    return ChatHistory()


def _skchat_home() -> Path:
    """Resolve the skchat home dir, honouring SKCHAT_HOME (tests sandbox it)."""
    return Path(os.environ.get("SKCHAT_HOME", str(Path.home() / ".skchat")))


def _load_group_by_id(gid: str):
    """Load a GroupChat by id from this agent's ``<home>/groups/<gid>.json``.

    Returns the ``GroupChat`` or ``None`` when *gid* names no group here (so a
    normal 1:1 recipient falls through to the DM send path).
    """
    if not gid:
        return None
    from .group import GroupChat

    path = _skchat_home() / "groups" / f"{gid}.json"
    if not path.exists():
        return None
    try:
        return GroupChat.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


# Transfer ids are path components served from disk — restrict to a safe charset
# (no slashes / dotdot) so a request can never escape the per-subdir base.
_TID_RE = _re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_transfer_dir(transfer_id: str, sub: str) -> Optional[Path]:
    """Resolve <home>/<sub>/<transfer_id>, guarding against path traversal.

    Returns the directory only if the transfer_id is well-formed and the
    resolved path stays under the base; otherwise None.
    """
    if not _TID_RE.match(transfer_id):
        return None
    base = (_skchat_home() / sub).resolve()
    target = (base / transfer_id).resolve()
    if base not in target.parents and target != base:
        return None
    return target if target.exists() else None


def _attachment_service():
    """Build an AttachmentService bound to a real FileTransferService."""
    from .attachments import AttachmentService
    from .files import FileTransferService

    ident = _get_identity()
    fs = FileTransferService(ident)
    return AttachmentService(ident, _get_history(), fs)


def _get_transport(identity: str):
    try:
        from skcomms import SKComms

        from .transport import ChatTransport

        comm = SKComms.from_config()
        # from_config wires ChatCrypto → DM ratchet can seal (card 3d0a3fef); the
        # bare constructor left crypto=None and sent plaintext to ratchet peers.
        return ChatTransport.from_config(skcomms=comm, history=_get_history(), identity=identity)
    except Exception as e:
        logger.warning("webui.py: %s", e)
        return None


def _display_name(uri: str) -> str:
    if not uri:
        return ""
    try:
        from .identity_bridge import resolve_display_name

        return resolve_display_name(uri)
    except Exception as e:
        logger.warning("webui.py: %s", e)
        pass
    try:
        local = uri.split(":", 1)[1] if ":" in uri else uri
        return (local.split("@", 1)[0] if "@" in local else local).capitalize()
    except Exception as e:
        logger.warning("webui.py: %s", e)
        return uri


def _msg_css(sender: str, my_identity: str) -> str:
    if sender == my_identity:
        return "self"
    lower = sender.lower()
    if "lumina" in lower:
        return "lumina"
    if "chef" in lower:
        return "chef"
    return ""


# ── HTML ──────────────────────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>SKChat</title>
<script src="https://unpkg.com/htmx.org@1.9.10"></script>
<style>
*{box-sizing:border-box}
body{background:#0a0a0a;color:#e0e0e0;font-family:monospace;margin:0}
#chat{height:calc(100vh - 52px);overflow-y:auto;padding:14px 18px;display:flex;flex-direction:column;gap:2px}
.msg .ts{color:#444;font-size:.8em;margin-right:5px}
.msg .who{font-weight:bold;margin-right:6px}
.msg .who{color:#4a9eff}
.self  .who{color:#4ade80}
.lumina .who{color:#c084fc}
.chef  .who{color:#fbbf24}
#bar{position:fixed;bottom:0;width:100%;background:#111;padding:8px 12px;display:flex;gap:8px;border-top:1px solid #1e1e1e}
select,input[type=text]{background:#1a1a1a;border:1px solid #2a2a2a;color:#e0e0e0;padding:6px 8px;font-family:monospace}
select{min-width:170px}input[type=text]{flex:1}
button{background:#1d4ed8;color:#fff;border:none;padding:6px 16px;cursor:pointer;font-family:monospace}
button:hover{background:#2563eb}
#ws-dot{position:fixed;top:5px;right:8px;font-size:.75em;color:#333}
#ws-dot.live{color:#4ade80}
.att-img{max-width:240px;border-radius:8px;display:block;margin-top:4px}
.att-file{display:inline-block;margin-top:4px;color:#7dd3fc;text-decoration:none}
.att-file:hover{text-decoration:underline}
#attach-btn{background:#374151}
#attach-btn:hover{background:#4b5563}
#upload-progress{position:fixed;bottom:46px;width:100%;background:#111;padding:4px 12px;font-size:.8em;color:#9ca3af;border-top:1px solid #1e1e1e}
#upload-progress progress{width:200px;vertical-align:middle;margin-right:8px}
</style>
</head>
<body>
<span id="ws-dot" title="WebSocket status">&#9679; ws</span>
<div id="chat" hx-get="/messages" hx-trigger="load" hx-swap="innerHTML"></div>
<form id="bar"
      hx-post="/send" hx-target="#chat" hx-swap="innerHTML"
      hx-on::after-request="this.querySelector('input[type=text]').value=''">
  <select name="recipient" id="recipient-sel">
    <option value="capauth:lumina@skworld.io">@Lumina</option>
    <option value="d4f3281e-fa92-474c-a8cd-f0a2a4c31c33">skworld-team</option>
  </select>
  <input type="text" name="content" placeholder="Message\u2026" autofocus autocomplete="off">
  <input type="file" id="file-input" multiple style="display:none">
  <button type="button" id="attach-btn" title="Attach">\U0001f4ce</button>
  <button type="submit">Send</button>
</form>
<div id="upload-progress" style="display:none"><progress id="up-bar" max="100" value="0"></progress> <span id="up-label"></span></div>
<script>
(function(){
  var fi = document.getElementById('file-input');
  document.getElementById('attach-btn').onclick = function(){ fi.click(); };
  function recipient(){ return document.getElementById('recipient-sel').value; }
  function caption(){ var c = document.querySelector('input[name=content]'); return c ? c.value : ''; }
  function uploadFiles(files){
    var prog = document.getElementById('upload-progress');
    var bar = document.getElementById('up-bar'), label = document.getElementById('up-label');
    var list = Array.prototype.slice.call(files);
    var chain = Promise.resolve();
    list.forEach(function(f){
      chain = chain.then(function(){
        return new Promise(function(res){
          prog.style.display='block'; bar.value=0; label.textContent='Uploading '+f.name+'\u2026';
          var fd = new FormData();
          fd.append('recipient', recipient());
          fd.append('caption', caption());
          fd.append('file', f);
          var xhr = new XMLHttpRequest();
          xhr.open('POST','/upload');
          xhr.upload.onprogress = function(e){ if(e.lengthComputable) bar.value = Math.round(100*e.loaded/e.total); };
          xhr.onload = function(){ res(); };
          xhr.onerror = function(){ res(); };
          xhr.send(fd);
        });
      });
    });
    chain.then(function(){
      prog.style.display='none';
      if (window.htmx) htmx.ajax('GET','/messages',{target:'#chat',swap:'innerHTML'});
    });
  }
  fi.onchange = function(){ if (fi.files.length) uploadFiles(fi.files); fi.value=''; };
  var chat = document.getElementById('chat');
  ['dragover','drop'].forEach(function(ev){ chat.addEventListener(ev, function(e){ e.preventDefault(); }); });
  chat.addEventListener('drop', function(e){ if(e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files); });
  document.addEventListener('paste', function(e){
    var items = (e.clipboardData && e.clipboardData.items) ? Array.prototype.slice.call(e.clipboardData.items) : [];
    var imgs = items.filter(function(i){ return i.type.indexOf('image/')===0; })
                    .map(function(i){ return i.getAsFile(); })
                    .filter(Boolean);
    if(imgs.length) uploadFiles(imgs);
  });
})();
</script>
<script>
(function(){
  var ws, rtimer;
  var dot = document.getElementById('ws-dot');
  function connect(){
    clearTimeout(rtimer);
    try { ws = new WebSocket('ws://'+location.host+'/ws/chat'); } catch(e){ return; }
    ws.onopen = function(){ dot.className='live'; };
    ws.onmessage = function(e){
      var msg;
      try { msg = JSON.parse(e.data); } catch(_){ return; }
      if(msg.type === 'new'){
        htmx.ajax('GET', '/messages', {target:'#chat', swap:'innerHTML'});
      }
    };
    ws.onclose = function(){ dot.className=''; rtimer = setTimeout(connect, 4000); };
  }
  connect();
  // Populate known groups in recipient selector
  fetch('/groups').then(function(r){ return r.json(); }).then(function(gs){
    var sel = document.getElementById('recipient-sel');
    gs.forEach(function(g){
      var o = document.createElement('option');
      o.value = g.id; o.textContent = g.name + ' (group)';
      sel.appendChild(o);
    });
  }).catch(function(){});
})();
</script>
</body>
</html>"""


# ── message rendering ─────────────────────────────────────────────────────────


def _render_messages(history, identity: str) -> str:
    try:
        msgs = history.load(limit=100)
    except Exception as e:
        logger.warning("webui.py: %s", e)
        msgs = []

    parts: list[str] = []
    for m in reversed(msgs):  # oldest-first for display
        if hasattr(m, "sender"):
            sender = m.sender
            content = m.content
            ts_raw = getattr(m, "timestamp", None)
        elif isinstance(m, dict):
            sender = m.get("sender", "?")
            content = m.get("content", "")
            ts_raw = m.get("timestamp")
        else:
            continue

        ts_str = ""
        if ts_raw:
            try:
                if isinstance(ts_raw, str):
                    ts_raw = datetime.fromisoformat(ts_raw)
                ts_str = ts_raw.strftime("%H:%M")
            except Exception as e:
                logger.warning("webui.py: %s", e)
                pass

        css = _msg_css(sender, identity)
        name = _display_name(sender)
        safe = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        att_html = ""
        for att in getattr(m, "attachments", []) or []:
            fname = att.filename.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if att.mime_type.startswith("image/") and att.thumbnail_id:
                att_html += (
                    f'<a href="/file/{att.transfer_id}" target="_blank">'
                    f'<img class="att-img" src="/file/{att.transfer_id}/thumb" '
                    f'alt="{fname}" loading="lazy"></a>'
                )
            else:
                kb = max(1, att.size // 1024)
                att_html += (
                    f'<a class="att-file" href="/file/{att.transfer_id}">'
                    f"\U0001f4c4 {fname} · {kb} KB · {att.mime_type}</a>"
                )

        parts.append(
            f'<div class="msg {css}">'
            f'<span class="ts">{ts_str}</span>'
            f'<span class="who">{name}</span>'
            f'<span class="text">{safe}</span>'
            f"{att_html}"
            f"</div>"
        )
    return "\n".join(parts)


# ── routes ────────────────────────────────────────────────────────────────────


@app.get("/")
async def index() -> RedirectResponse:
    return RedirectResponse(url="/voice", status_code=307)


@app.get("/legacy", response_class=HTMLResponse)
async def legacy_index() -> HTMLResponse:
    return HTMLResponse(_HTML)


@app.get("/pair/qr")
def pair_qr(sy: str = "1", ts: str = "1", https: str = "1", embed: str = "0"):
    import io

    import segno
    from skcomms import pairing

    def _on(v):
        return str(v).lower() not in ("0", "false", "no", "off", "")

    def _build(embed_key: bool):
        b = pairing.bundle_from_self(embed_key=embed_key)
        if not _on(sy):
            b.syncthing_device_id = None
        if not _on(ts):
            b.tailscale = None
        if not _on(https):
            b.https = None
        return b, pairing.to_skp_uri(b)

    bundle, uri = _build(_on(embed))
    warning = None
    try:
        # error="l" = max data capacity (a QR tops out ~2953 bytes).
        qr = segno.make(uri, error="l")
    except Exception as exc:  # segno.encoder.DataOverflowError — key too big to embed
        logger.debug("QR encode overflowed (%s: %s); using compact QR", type(exc).__name__, exc)
        bundle, uri = _build(False)  # fall back to a compact QR
        qr = segno.make(uri, error="l")
        warning = (
            "Public key too large to embed in a QR — using a compact "
            "code (the peer fetches + verifies the key on accept)."
        )
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=5)
    return {
        "uri": uri,
        "svg": buf.getvalue().decode("utf-8"),
        "fqid": bundle.fqid,
        "fingerprint": bundle.fingerprint,
        "embedded": bundle.pubkey is not None,
        "warning": warning,
    }


_PAIR_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>skchat — Pair</title>
<style>body{font-family:system-ui;max-width:480px;margin:2rem auto;text-align:center}
#pair-qr svg{width:280px;height:280px} label{display:block;text-align:left;margin:.3rem 0}
#pair-uri{word-break:break-all;font-size:.8rem;color:#555;margin-top:1rem}</style></head>
<body><h2>Pair a device</h2><p>Scan this with another agent's <code>skchat</code>.</p>
<div id="pair-qr">loading…</div>
<form id="caps">
 <label><input type="checkbox" name="sy" checked> Share Syncthing device</label>
 <label><input type="checkbox" name="ts" checked> Share Tailscale address</label>
 <label><input type="checkbox" name="https" checked> Share HTTPS endpoint</label>
 <label><input type="checkbox" name="embed"> Embed public key (offline-capable, bigger QR)</label>
</form>
<div id="pair-uri"></div><button id="copy">Copy link</button>
<script>
function flag(n){return document.querySelector('input[name='+n+']').checked?'1':'0';}
function refresh(){
 var q='/pair/qr?sy='+flag('sy')+'&ts='+flag('ts')+'&https='+flag('https')+'&embed='+flag('embed');
 fetch(q).then(function(r){return r.json();}).then(function(d){
   document.getElementById('pair-qr').innerHTML=d.svg;
   document.getElementById('pair-uri').textContent=d.uri;
   window._uri=d.uri;});
}
document.getElementById('caps').addEventListener('change',refresh);
document.getElementById('copy').onclick=function(){if(window._uri)navigator.clipboard.writeText(window._uri);};
refresh();
</script>
<div id="ring-banner" style="display:none;position:fixed;top:0;left:0;right:0;
  background:#143;color:#fff;padding:12px;text-align:center;z-index:9999"></div>
<div id="peer-list" style="max-width:520px;margin:12px auto;font-family:sans-serif"></div>
<script>
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
async function loadPeers(){
  try{
    const r = await fetch('/call/peers'); if(!r.ok)return;
    const {peers} = await r.json();
    const el = document.getElementById('peer-list');
    if(!peers || !peers.length){ el.innerHTML = '<em>No paired peers yet.</em>'; return; }
    el.innerHTML = '<h3>Paired peers</h3>' + peers.map(p =>
      '<div style="padding:6px;border-bottom:1px solid #ccc;display:flex;'
      +'justify-content:space-between;align-items:center">'
      +'<span>'+esc(p.fqid)+'</span>'
      +'<button data-fqid="'+esc(p.fqid)+'" class="call-btn">📞 Call</button></div>'
    ).join('');
    el.querySelectorAll('.call-btn').forEach(function(btn){
      btn.addEventListener('click', function(){ callPeer(this.dataset.fqid); });
    });
  }catch(e){}
}
loadPeers();
</script>
<script>
async function callPeer(fqid){
  const r = await fetch('/call/start',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({peer:fqid})});
  if(!r.ok){alert('call failed: '+r.status);return;}
  const d = await r.json();
  location.href = '/livekit?room='+encodeURIComponent(d.room)
    +'&identity='+encodeURIComponent(d.identity)
    +'&token='+encodeURIComponent(d.token);
}
async function answerPeer(fqid){
  const r = await fetch('/call/answer',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({peer:fqid})});
  if(!r.ok){alert('answer failed: '+r.status);return;}
  const d = await r.json();
  location.href = '/livekit?room='+encodeURIComponent(d.room)
    +'&identity='+encodeURIComponent(d.identity)
    +'&token='+encodeURIComponent(d.token);
}
async function pollRing(){
  try{
    const r = await fetch('/call/incoming'); if(!r.ok)return;
    const {invites} = await r.json();
    const b = document.getElementById('ring-banner');
    if(invites && invites.length){
      const inv = invites[0];
      b.innerHTML = '📞 Incoming call from '+esc(inv.from_fqid)+' '
        +'<button class="answer-btn" data-fqid="'+esc(inv.from_fqid)+'">Accept</button>';
      b.querySelector('.answer-btn').addEventListener('click', function(){ answerPeer(this.dataset.fqid); });
      b.style.display='block';
    } else { b.style.display='none'; }
  }catch(e){}
}
setInterval(pollRing, 4000); pollRing();
</script></body></html>"""


@app.get("/pair", response_class=HTMLResponse)
async def pair_page() -> HTMLResponse:
    return HTMLResponse(_PAIR_HTML)


@app.post("/pair/accept")
async def pair_accept(payload: dict = Body(...)):
    """Accept a scanned/pasted skp:// pairing URI.

    Delegates to ``skcomms.pairing.accept_pairing`` which securely verifies the
    key fingerprint before TOFU-adding the peer; a fingerprint mismatch or an
    unresolvable key raises ``ValueError`` → mapped to HTTP 400.
    """
    from skcomms import pairing

    from .pairing_gate import gate_required, get_gate

    uri = (payload or {}).get("uri", "").strip()
    if not uri:
        raise HTTPException(status_code=400, detail="missing 'uri'")
    # When exposed publicly (Tailscale Funnel), require an operator-opened,
    # time-boxed pairing window + nonce + rate limit. Tailnet usage is unchanged.
    gate = get_gate()
    if gate_required():
        ok, reason = gate.check((payload or {}).get("nonce", ""))
        if not ok:
            code = 429 if "rate limited" in reason else 403
            raise HTTPException(status_code=code, detail=reason)
    try:
        res = pairing.accept_pairing(uri)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if gate_required():
        gate.consume()
    # peer list / a system note may change
    asyncio.create_task(_ws_broadcast({"type": "new"}))
    return res


@app.post("/pair/open")
async def pair_open():
    """Operator opens a time-boxed pairing window; returns a nonce.

    Keep this endpoint tailnet-only — do NOT expose it over Funnel. Only
    ``/pair/scan`` + ``/pair/accept`` should be public; the operator opens the
    window from the trusted side, and the remote device presents the nonce.
    """
    import os

    from .pairing_gate import get_gate

    info = get_gate().open_window()
    # Ready-to-share public scan URL (carries the gate nonce). The remote opens
    # this, scans the skp:// QR, and the page posts {uri, nonce} to /pair/accept.
    base = os.getenv("SKCHAT_FUNNEL_PUBLIC_URL", "").rstrip("/")
    if base:
        info["scan_url"] = f"{base}/pair/scan?gate={info['nonce']}"
    return info


_SCAN_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>skchat — Scan to Pair</title>
<style>body{font-family:system-ui;max-width:480px;margin:2rem auto;text-align:center}
video{width:300px;max-width:100%;border-radius:8px;background:#111}
#manual{width:100%;font-size:.8rem} #result{margin-top:1rem;font-weight:600}
.err{color:#b00} .ok{color:#070}</style></head>
<body><h2>Scan to pair</h2>
<video id="cam" autoplay playsinline muted></video>
<p id="camnote" style="color:#777"></p>
<p>…or paste an <code>skp://</code> link:</p>
<input id="manual" placeholder="skp://pair?v=1&fqid=…"><button id="go">Pair</button>
<div id="result"></div>
<script>
function show(msg,ok){var r=document.getElementById('result');r.textContent=msg;r.className=ok?'ok':'err';}
function accept(uri){
 if(!uri||uri.indexOf('skp://')!==0){show('Not an skp:// pairing link',false);return;}
 var gate=new URLSearchParams(location.search).get('gate')||'';
 fetch('/pair/accept',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({uri:uri,nonce:gate})})
  .then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d};});})
  .then(function(x){show(x.ok?('Paired with '+x.d.fqid):(x.d.detail||'Pairing failed'),x.ok);});
}
document.getElementById('go').onclick=function(){accept(document.getElementById('manual').value.trim());};
(function(){
 var note=document.getElementById('camnote');
 if(!('BarcodeDetector' in window)){note.textContent='Camera QR scan not supported here — paste the link instead.';return;}
 navigator.mediaDevices.getUserMedia({video:{facingMode:'environment'}}).then(function(stream){
   var v=document.getElementById('cam');v.srcObject=stream;
   var det=new BarcodeDetector({formats:['qr_code']});var done=false;
   var tick=function(){ if(done)return; det.detect(v).then(function(codes){
     for(var i=0;i<codes.length;i++){var val=codes[i].rawValue||'';if(val.indexOf('skp://')===0){done=true;stream.getTracks().forEach(function(t){t.stop();});accept(val);return;}}
     requestAnimationFrame(tick);}).catch(function(){requestAnimationFrame(tick);});};
   requestAnimationFrame(tick);
 }).catch(function(){note.textContent='Camera unavailable — paste the link instead.';});
})();
</script></body></html>"""


@app.get("/pair/scan", response_class=HTMLResponse)
async def pair_scan_page() -> HTMLResponse:
    return HTMLResponse(_SCAN_HTML)


@app.get("/messages", response_class=HTMLResponse)
async def messages() -> HTMLResponse:
    identity = _get_identity()
    history = _get_history()
    return HTMLResponse(_render_messages(history, identity))


@app.post("/send", response_class=HTMLResponse)
async def send(recipient: str = Form(...), content: str = Form(...)) -> HTMLResponse:
    if content.strip():
        identity = _get_identity()
        transport = _get_transport(identity)
        if transport:
            # send_and_store can now raise ConfidentialityError: with crypto wired
            # (card 3d0a3fef) the DM ratchet fails closed rather than downgrading to
            # plaintext for a live-ratchet peer whose seal fails. Do NOT let that
            # 500 the route; persist locally as PENDING so the message is not lost.
            try:
                transport.send_and_store(recipient=recipient, content=content)
            except Exception as exc:  # noqa: BLE001
                logger.warning("webui /send: delivery to %s failed: %s", recipient, exc)
                from .models import ChatMessage, DeliveryStatus

                # Mark PENDING so the un-sent message is not indistinguishable from a
                # delivered one in the rendered thread (card 3d0a3fef follow-up).
                _get_history().save(
                    ChatMessage(
                        sender=identity,
                        recipient=recipient,
                        content=content,
                        delivery_status=DeliveryStatus.PENDING,
                    )
                )
        else:
            from .models import ChatMessage, DeliveryStatus

            msg = ChatMessage(
                sender=identity,
                recipient=recipient,
                content=content,
                delivery_status=DeliveryStatus.PENDING,
            )
            _get_history().save(msg)
        # Notify WS clients so they refresh
        asyncio.create_task(_ws_broadcast({"type": "new"}))
    identity = _get_identity()
    history = _get_history()
    return HTMLResponse(_render_messages(history, identity))


@app.post("/api/send")
async def api_send(
    payload: dict = Body(...),
    _auth: None = Depends(require_dataplane_auth),
) -> JSONResponse:
    """JSON send path for native clients (Flutter app).

    The legacy HTML ``POST /send`` (form body -> server-rendered HTML) is left
    untouched for the web UI.  This route runs the SAME send_and_store transport
    path but speaks JSON in and JSON out, and broadcasts the identical
    ``{"type": "new"}`` signal to ``/ws/chat`` so open web clients still refresh.

    Native client contract (the three pieces a Flutter port consumes):
      * Send   -> POST /api/send  body {"recipient": str, "content": str}
                  returns {"ok": bool, "id": str, "recipient": str, "ts": iso8601}
                  (400 if content is blank, 422 if recipient is missing).
      * Inbox  -> GET /inbox returns a JSON array of messages, each:
                  {id, sender, recipient, content, timestamp, delivery_status, thread_id}.
      * Live   -> WS /ws/chat pushes {"type": "new"} whenever a message is
                  sent or received; the client re-fetches /inbox on that signal.

    Args:
        payload: JSON object with ``recipient`` and ``content`` keys.

    Returns:
        JSONResponse: {ok, id, recipient, ts}.
    """
    recipient = (payload or {}).get("recipient")
    content = (payload or {}).get("content")
    if not isinstance(recipient, str) or not recipient:
        raise HTTPException(status_code=422, detail="recipient is required")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=400, detail="content is required")

    from .models import ChatMessage

    identity = _get_identity()
    # Build the message up front so the JSON response carries the exact id/ts
    # that gets stored, regardless of which send path runs below.
    msg = ChatMessage(sender=identity, recipient=recipient, content=content)

    transport = _get_transport(identity)

    # Group send: a "group:<id>" recipient (or a bare id matching a group record)
    # must fan out to each member — the 1:1 send_and_store path can only reach a
    # single peer, so a group recipient would silently go nowhere. Deliver to each
    # non-self member via the DM transport with thread_id=<gid> (the same path the
    # daemon's group responder uses), and persist one group-thread copy locally.
    _gid = recipient[6:] if recipient.startswith("group:") else recipient
    _grp = _load_group_by_id(_gid)
    if _grp is not None:
        history = _get_history()
        own = ChatMessage(
            sender=identity,
            recipient=f"group:{_grp.id}",
            content=content,
            thread_id=_grp.id,
        )
        history.save(own)
        for member in _grp.members:
            if member.identity_uri == identity:
                continue
            try:
                if transport:
                    transport.send_message(
                        ChatMessage(
                            sender=identity,
                            recipient=member.identity_uri,
                            content=content,
                            thread_id=_grp.id,
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("group send to %s failed: %s", member.identity_uri, exc)
        asyncio.create_task(_ws_broadcast({"type": "new"}))
        return JSONResponse(
            {
                "ok": True,
                "id": own.id,
                "recipient": recipient,
                "ts": own.timestamp.isoformat() if own.timestamp else None,
            }
        )

    if transport:
        # Same transport path as the HTML /send route. Fail-closed ConfidentialityError
        # (card 3d0a3fef) must not 500 /api/send: persist locally as PENDING so the
        # native client sees the message queued (not delivered) on its next refresh
        # rather than a bare server error. The JSON contract is kept exactly (the
        # Flutter app decodes it strictly); delivery state is surfaced via the
        # persisted delivery_status the client reads back from history, not a new
        # response field.
        try:
            transport.send_and_store(recipient=recipient, content=content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("webui /api/send: delivery to %s failed: %s", recipient, exc)
            from .models import DeliveryStatus

            msg.delivery_status = DeliveryStatus.PENDING
            _get_history().save(msg)
    else:
        from .models import DeliveryStatus

        msg.delivery_status = DeliveryStatus.PENDING
        _get_history().save(msg)

    # Notify WS clients so they refresh (identical signal to the HTML /send route).
    asyncio.create_task(_ws_broadcast({"type": "new"}))

    return JSONResponse(
        {
            "ok": True,
            "id": msg.id,
            "recipient": recipient,
            "ts": msg.timestamp.isoformat() if msg.timestamp else None,
        }
    )


@app.post("/upload")
async def upload(
    recipient: str = Form(...),
    caption: str = Form(""),
    file: UploadFile = File(...),
) -> JSONResponse:
    """Accept a multipart file, stage it, and send it as a chat attachment."""
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large")
    home = _skchat_home()
    staged = home / "uploads" / _uuid.uuid4().hex / (file.filename or "upload.bin")
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(data)
    svc = _attachment_service()
    msg = svc.send_attachment(recipient, staged, caption=caption or None)
    asyncio.create_task(_ws_broadcast({"type": "new"}))
    return JSONResponse(
        {
            "id": msg.id,
            "transfer_id": msg.attachments[0].transfer_id,
            "filename": msg.attachments[0].filename,
        }
    )


#: server transfer status -> the client's 'pending|in_progress|completed|failed'
_FILE_STATUS_MAP = {
    "complete": "completed",
    "completed": "completed",
    "failed": "failed",
    "sending": "in_progress",
    "receiving": "in_progress",
    "preparing": "pending",
    "pending": "pending",
}


@app.get("/api/v1/file_status")
def file_status(transfer_id: str) -> JSONResponse:
    """Poll a file transfer's progress (the file-transfer bubble polls this /2s).

    Reads the persisted ``~/.skchat/transfers/<transfer_id>.json`` and maps it to
    the client ``FileTransferStatus`` shape. 404 if no such transfer. Path
    traversal is guarded (``_TID_RE`` + resolve-under-base).
    """
    if not _TID_RE.match(transfer_id):
        raise HTTPException(status_code=404, detail="not found")
    base = (_skchat_home() / "transfers").resolve()
    meta_path = (base / f"{transfer_id}.json").resolve()
    if base not in meta_path.parents or not meta_path.exists():
        raise HTTPException(status_code=404, detail="not found")
    try:
        meta = json.loads(meta_path.read_text())
    except Exception:
        raise HTTPException(status_code=404, detail="not found")

    status = _FILE_STATUS_MAP.get(str(meta.get("status", "")).lower(), "pending")
    file_size = int(meta.get("file_size", 0) or 0)
    total = int(meta.get("total_chunks", 0) or 0)
    done = int(meta.get("chunks_sent", 0) or 0)  # outbound; inbound unknown here
    if status == "completed":
        bytes_transferred = file_size
    elif total > 0 and file_size > 0:
        bytes_transferred = min(file_size, round(file_size * done / total))
    else:
        bytes_transferred = 0
    return JSONResponse(
        {
            "transfer_id": transfer_id,
            "status": status,
            "file_name": meta.get("filename", ""),
            "file_size": file_size,
            "bytes_transferred": bytes_transferred,
            "speed_bps": 0,
            "error_message": meta.get("error") or meta.get("error_message"),
        }
    )


@app.get("/file/{transfer_id}")
def download_file(transfer_id: str) -> FileResponse:
    """Download the file for a completed transfer (path-traversal guarded)."""
    d = _safe_transfer_dir(transfer_id, "received") or _safe_transfer_dir(transfer_id, "uploads")
    if d is None:
        raise HTTPException(status_code=404, detail="not found")
    files = [p for p in d.rglob("*") if p.is_file() and p.name != "thumb.webp"]
    if not files:
        raise HTTPException(status_code=404, detail="empty")
    f = files[0]
    return FileResponse(
        str(f),
        filename=f.name,
        headers={"Content-Disposition": f'attachment; filename="{f.name}"'},
    )


@app.get("/file/{transfer_id}/thumb")
def file_thumb(transfer_id: str) -> FileResponse:
    """Serve the WebP thumbnail for an image transfer, if one exists."""
    for sub in ("received", "thumbnails", "uploads"):
        d = _safe_transfer_dir(transfer_id, sub)
        if d and (d / "thumb.webp").exists():
            return FileResponse(str(d / "thumb.webp"), media_type="image/webp")
    raise HTTPException(status_code=404, detail="no thumbnail")


@app.get("/inbox")
async def inbox(limit: int = 100, since_minutes: int = 1440) -> JSONResponse:
    """Return recent messages as JSON.

    Part of the native-client (Flutter) contract alongside ``POST /api/send``
    and the ``/ws/chat`` ``{"type": "new"}`` signal.  Each element:
    {id, sender, recipient, content, timestamp, delivery_status, thread_id}.

    Args:
        limit: Max messages to return.
        since_minutes: Look-back window (0 = all).
    """
    history = _get_history()
    since: Optional[datetime] = (
        datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
        if since_minutes > 0
        else None
    )
    msgs = history.load(since=since, limit=limit)
    return JSONResponse(
        [
            {
                "id": m.id,
                "sender": m.sender,
                "recipient": m.recipient,
                "content": m.content,
                "timestamp": m.timestamp.isoformat() if m.timestamp else None,
                "delivery_status": m.delivery_status.value,
                "thread_id": m.thread_id,
            }
            for m in msgs
        ]
    )


@app.get("/groups")
async def groups() -> JSONResponse:
    """Return known groups loaded from ~/.skchat/groups/*.json.

    Each member is enriched with peer-registry data (entity_type, fingerprint,
    soul-derived display name) so the UI can distinguish humans from agents
    without bare-URI rendering.
    """
    from .group import GroupChat
    from .peer_discovery import PeerDiscovery

    discovery = PeerDiscovery()
    groups_dir = _SKCHAT_HOME / "groups"
    result: list[dict] = []
    if not groups_dir.exists():
        return JSONResponse(result)

    for f in sorted(groups_dir.glob("*.json")):
        try:
            grp = GroupChat.model_validate_json(f.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("webui.py: %s", e)
            continue

        members_out = []
        for m in grp.members:
            peer = discovery.get_peer(m.identity_uri) or {}
            entity_type = peer.get("entity_type") or m.participant_type.value
            display_name = (
                m.display_name or peer.get("name") or m.identity_uri.split(":")[-1].split("@")[0]
            )
            members_out.append(
                {
                    "uri": m.identity_uri,
                    "role": m.role.value,
                    "participant_type": m.participant_type.value,
                    "display_name": display_name,
                    "entity_type": entity_type,
                    "fingerprint": peer.get("fingerprint", ""),
                    "trust_level": peer.get("trust_level", "unknown"),
                }
            )

        result.append(
            {
                "id": grp.id,
                "name": grp.name,
                "description": grp.description,
                "member_count": grp.member_count,
                "members": members_out,
                "message_count": grp.message_count,
                "created_at": grp.created_at.isoformat(),
                "updated_at": grp.updated_at.isoformat(),
            }
        )
    return JSONResponse(result)


# ── WebSocket real-time push ───────────────────────────────────────────────────


@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket) -> None:
    """Real-time push channel. Clients receive {type: 'new'} when the history
    has new messages, and should re-fetch /messages in response."""
    await websocket.accept()
    _ws_connections.add(websocket)
    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=20)
                if data == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "heartbeat"}))
            except WebSocketDisconnect:
                break
    except Exception as e:
        logger.warning("webui.py: %s", e)
        pass
    finally:
        _ws_connections.discard(websocket)


# ── Background poller ─────────────────────────────────────────────────────────


async def _background_message_poller() -> None:
    """Poll JSONL history every 3 s; push {type:'new'} to WS clients when
    messages arrive after startup (e.g. daemon wrote them)."""
    global _last_push_dt
    _last_push_dt = datetime.now(timezone.utc)
    await asyncio.sleep(2)  # let app fully start
    while True:
        await asyncio.sleep(3)
        if not _ws_connections:
            continue
        try:
            history = _get_history()
            new_msgs = history.load(since=_last_push_dt, limit=20)
            if new_msgs:
                # load() is newest-first; update cutoff past the newest timestamp
                newest = new_msgs[0].timestamp
                if newest is not None:
                    if newest.tzinfo is None:
                        newest = newest.replace(tzinfo=timezone.utc)
                    _last_push_dt = newest + timedelta(microseconds=1)
                await _ws_broadcast({"type": "new", "count": len(new_msgs)})
        except Exception as e:
            logger.warning("webui.py: %s", e)
            pass


@app.on_event("startup")
async def _startup() -> None:
    asyncio.create_task(_background_message_poller())


# ── entry point ───────────────────────────────────────────────────────────────


def run(port: int = 8765, open_browser: bool = True, host: str = "") -> None:
    """Start the SKChat Web UI server (blocking)."""
    import uvicorn

    if not host:
        host = os.environ.get("SKCHAT_HOST", "127.0.0.1")
    if open_browser:
        webbrowser.open(f"http://localhost:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
