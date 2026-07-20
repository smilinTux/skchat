"""Classify (method, path) as gated (needs operator auth) or exempt.

Exempt = genuinely public or auth-bootstrap: health, static app, federation
inbound (POST /api/v1/inbox), signed discovery, invite/pair/guest join, livekit
signaling, the /api/v1/auth/* handshake itself, the guest-authed route
families mounted under /api/v1 (/api/v1/guest/*, /api/v1/mode-c/*), the public
PQ prekey directory (GET /api/v1/prekey*), and the pre-session UI bootstrap
reads (GET /api/v1/identity, GET /api/v1/capabilities). The guest/mode-c
families carry their OWN auth (guest-session JWT via ``_guest_session``, or
invite-token / operator-token gating) which the operator-session validator
does not accept, so they must stay exempt from this gate or every guest flow
would 401 once the flag is flipped on. Everything else under /api/v1 (plus the
sensitive webui routes, including raw attachment bytes under /file, the
unauthenticated file-viewer stream at /media/file, the coord/kanban proxy at
/api/board, and the channel-adapter health surface at /adapters) is gated.

WEBSOCKET BOUNDARY: this classifier (and the ``@app.middleware("http")`` gate
that consults it in webui.py) covers HTTP requests only. It does NOT cover
websocket routes (``/ws/*``). Today ``/ws/chat`` emits only
``{"type": "new", "count": N}`` refresh-signal metadata, no message content,
so the exposure is currently low, but the connection itself is unauthenticated.
Per-connection websocket auth is a tracked follow-up, not addressed here.
"""

from __future__ import annotations

_EXEMPT_EXACT = {
    ("GET", "/health"), ("GET", "/api/health"),
    ("GET", "/favicon.ico"),
    ("POST", "/api/v1/inbox"),                 # federation S2S inbound
    ("GET", "/api/v1/auth/challenge"),
    ("POST", "/api/v1/auth/session"),
    ("POST", "/api/v1/auth/enroll"),
    ("POST", "/api/v1/auth/enroll/open"),      # itself operator-gated in-route
    ("GET", "/api/v1/identity"),               # pre-session UI bootstrap
    ("GET", "/api/v1/capabilities"),           # pre-session UI bootstrap
}
_EXEMPT_PREFIX = (
    "/app", "/static", "/.well-known/",
    "/join", "/guest", "/pair", "/livekit", "/ws/",
    "/api/v1/guest", "/api/v1/mode-c",
)
# Method-aware exempt prefixes: (method, prefix) pairs anchored the same way as
# _EXEMPT_PREFIX, but scoped to one method. Used where the same path family must
# stay gated for one method and be public for another, e.g. the PQ prekey
# directory: peers fetch bundles unauthenticated (GET), but publishing a bundle
# (POST) is an operator-owned write and stays gated.
_EXEMPT_METHOD_PREFIX = {
    ("GET", "/api/v1/prekey"),
}
# Gated prefixes outside /api/v1 and /api/send: raw attachment bytes and the
# coord/adapter surfaces. Anchored to path-segment boundaries like the exempt
# prefixes above, so e.g. a future /file-uploads route is not silently swept in.
_GATED_PREFIX = (
    "/file", "/adapters",
)
_GATED_EXACT = {
    "/api/board", "/media/file",
}


def _prefix_hit(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def is_gated(method: str, path: str) -> bool:
    method = method.upper()
    if (method, path) in _EXEMPT_EXACT:
        return False
    if path == "/":
        return False
    for p in _EXEMPT_PREFIX:
        if _prefix_hit(path, p):
            return False
    for m, p in _EXEMPT_METHOD_PREFIX:
        if method == m and _prefix_hit(path, p):
            return False
    if path in _GATED_EXACT:
        return True
    for p in _GATED_PREFIX:
        if _prefix_hit(path, p):
            return True
    return path.startswith("/api/v1") or path.startswith("/api/send") or path in (
        "/inbox", "/send", "/messages", "/groups", "/upload", "/agent/state")
