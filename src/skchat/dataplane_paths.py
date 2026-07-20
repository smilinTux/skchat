"""Classify (method, path) as gated (needs operator auth) or exempt.

Exempt = genuinely public or auth-bootstrap: health, static app, federation
inbound (POST /api/v1/inbox), signed discovery, invite/pair/guest join, livekit
signaling, and the /api/v1/auth/* handshake itself. Everything else under
/api/v1 (plus the sensitive webui routes) is gated.
"""

from __future__ import annotations

_EXEMPT_EXACT = {
    ("GET", "/health"), ("GET", "/api/health"),
    ("POST", "/api/v1/inbox"),                 # federation S2S inbound
    ("GET", "/api/v1/auth/challenge"),
    ("POST", "/api/v1/auth/session"),
    ("POST", "/api/v1/auth/enroll"),
    ("POST", "/api/v1/auth/enroll/open"),      # itself operator-gated in-route
}
_EXEMPT_PREFIX = (
    "/app", "/static", "/favicon", "/.well-known/",
    "/join", "/guest", "/pair", "/livekit", "/ws/",
)


def is_gated(method: str, path: str) -> bool:
    method = method.upper()
    if (method, path) in _EXEMPT_EXACT:
        return False
    if path == "/":
        return False
    for p in _EXEMPT_PREFIX:
        if path.startswith(p):
            return False
    return path.startswith("/api/v1") or path.startswith("/api/send") or path in (
        "/inbox", "/send", "/messages", "/groups", "/upload", "/agent/state")
