"""Deep-link helpers for handing native call/conf joins to the Flutter app.

The Flutter web app is served at ``/app/`` (see ``conf.routes.flutter_app``) and
uses hash-based routing, so an in-app route ``/conf?...`` is reached via the URL
fragment ``/app/#/conf?...``. These helpers build that deep link from a freshly
minted guest/sovereign/conf token so a shared invite lands in the NATIVE conf
experience (with its panels) instead of the standalone web ``livekit.html`` page.

The web pages remain reachable as a fallback: a ``?web=1`` query flag on the
landing URL keeps the legacy ``livekit.html`` / conf redirect (see
:func:`wants_web_fallback`). This makes the hand-off minimal and fully reversible.
"""

from __future__ import annotations

from urllib.parse import urlencode

# Base path the Flutter web app is mounted at (hash-routed SPA). The conf route
# lives in the URL fragment, so the join deep link is ``/app/#/conf?...``.
APP_CONF_BASE = "/app/#/conf"


def conf_app_link(
    room: str,
    *,
    token: str = "",
    url: str = "",
    identity: str = "",
    display: str = "",
) -> str:
    """Build the native ``/app/#/conf?...`` deep link for a conference join.

    Only non-empty fields are included. ``token`` + ``url`` carry a pre-minted,
    role-scoped LiveKit credential so the app connects straight to media via
    ``LiveKitCallService.connectWithToken``; a bare ``room`` hands the app the
    room to mint against with the signed-in identity.
    """
    params: dict[str, str] = {"room": room}
    if token:
        params["token"] = token
    if url:
        params["url"] = url
    if identity:
        params["identity"] = identity
    if display:
        params["display"] = display
    return f"{APP_CONF_BASE}?{urlencode(params)}"


def wants_web_fallback(request) -> bool:
    """True when the landing URL asks to keep the legacy web client (``?web=1``).

    Defensive: any missing/odd ``request`` shape falls back to the native app.
    """
    try:
        return request.query_params.get("web", "") == "1"
    except Exception:  # pragma: no cover - defensive
        return False
