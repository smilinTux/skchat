"""Guest join — one-link ephemeral call access (D1).

Flow
----
1. Operator calls ``InviteIssuer.create_invite(room, ...)`` → gets a signed JWT
   (the *invite token*) + a shareable URL.
2. Operator sends the URL out-of-band. The URL is the only secret.
3. Guest GETs ``/join/<room>?invite=<token>`` — served a tiny join HTML page.
4. Guest POSTs ``/guest/join`` with ``{room, invite_token, display_name}``.
   ``InviteVerifier.join()`` validates the invite → returns a ``GuestToken``
   dataclass + a ready-to-use LiveKit participant JWT.
5. Browser is redirected to ``/livekit/<room>?room=…&identity=…&token=…``;
   livekit.html auto-connects using the pre-minted token.

Security properties
-------------------
- Invite tokens are HS256 JWTs signed with ``SKCHAT_GUEST_TOKEN_SECRET``.
- Token is room-scoped (``room`` claim); cannot be used to join another room.
- Guest identity in LiveKit is always server-assigned (``guest:<jti[:8]>``).
- Revocation: ``revoke_invite(jti)`` adds to an in-memory set; a revoked token
  is rejected before any LiveKit call is made. (P1: replace with Postgres table.)
- Invalid/expired/revoked tokens all produce the same HTTP 401 + generic message
  (no oracle distinguishing expiry vs bad signature).

Route wiring (done)
-------------------
  from skchat.guest import register_guest_routes
  register_guest_routes(app)   # called from webui.py after the spaces/glossa block
"""

from __future__ import annotations

import html
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

# `from __future__ import annotations` turns the route handlers' ``request:
# Request`` hints into strings; FastAPI resolves them against this module's
# globals, so ``Request`` must be importable at module scope. Guarded so pure
# CLI usage (no FastAPI installed) still imports this module fine.
if TYPE_CHECKING:  # pragma: no cover
    from fastapi import Request
else:
    try:
        from fastapi import Request
    except ImportError:  # FastAPI not installed; routes are never registered.
        Request = None  # type: ignore[assignment,misc]

logger = logging.getLogger("skchat.guest")

# ── Config ────────────────────────────────────────────────────────────────────
_GUEST_SECRET_ENV = "SKCHAT_GUEST_TOKEN_SECRET"
_INVITE_TTL_ENV = "SKCHAT_INVITE_WINDOW_TTL"
_FUNNEL_URL_ENV = "SKCHAT_FUNNEL_PUBLIC_URL"
# Operator-auth for /guest/invite + /guest/revoke. A shared bearer secret lets the
# operator drive these from off-host (e.g. behind Funnel); when unset, access is
# restricted to loopback/private (tailnet) clients -- matching the P0 "tailnet-only"
# posture of the pairing routes. Public guests never touch these endpoints; they
# use the invite-JWT-gated /join/{room} + /guest/join only.
_OPERATOR_TOKEN_ENV = "SKCHAT_GUEST_OPERATOR_TOKEN"

# Private/loopback address prefixes trusted as "operator on the local network".
_PRIVATE_PREFIXES = (
    "127.",
    "10.",
    "192.168.",
    "100.",  # Tailscale CGNAT range (100.64.0.0/10)
    "::1",
    "fd",  # ULA / Tailscale IPv6
)

_DEFAULT_INVITE_TTL = 14400  # 4 hours
_MAX_INVITE_TTL = 28800  # 8 hours hard cap
_GUEST_PERMS = ["join", "chat_send", "publish_audio", "publish_camera"]

# ── Module-level revocation list (P0: in-memory; P1: Postgres) ───────────────
_revoked_jtis: set[str] = set()


def revoke_invite(jti: str) -> None:
    """Add a JTI to the revocation list. Revoked tokens are rejected at verify."""
    _revoked_jtis.add(jti)


def _is_revoked(jti: str) -> bool:
    return jti in _revoked_jtis


# -- Operator-auth gate (for /guest/invite + /guest/revoke) -------------------


def _client_is_private(request: object) -> bool:
    """True if the request originates from loopback or a private/tailnet IP."""
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client is not None else None
    if not host:
        # No client info (e.g. some TestClient configs) -> treat as untrusted.
        return False
    return host.startswith(_PRIVATE_PREFIXES)


def _require_operator(request: object) -> None:
    """Authorize an operator-only request, or raise HTTP 401/403.

    Policy (additive, P0 posture):
      * If ``SKCHAT_GUEST_OPERATOR_TOKEN`` is set, the caller MUST present it as
        a bearer token (``Authorization: Bearer <token>`` or ``X-Operator-Token``
        header) -- this makes the endpoints safe even if Funnel-exposed.
      * Otherwise, fall back to the pairing-route posture: only loopback/tailnet
        (private-IP) clients are allowed; public callers get 403.

    Public/anonymous callers are therefore never able to mint or revoke invites.
    """
    from fastapi import HTTPException

    token = os.getenv(_OPERATOR_TOKEN_ENV, "").strip()
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
    # No shared secret configured: trust only the local/tailnet network.
    if not _client_is_private(request):
        raise HTTPException(
            status_code=403,
            detail="operator endpoint is tailnet-only; set "
            f"{_OPERATOR_TOKEN_ENV} to allow authenticated remote access",
        )


# ── Errors ───────────────────────────────────────────────────────────────────


class GuestJoinError(Exception):
    """Raised when invite validation fails for any reason.

    The message is internal; callers MUST NOT expose it verbatim to the guest
    (use a generic "invalid or expired invite" HTTP 401 response).
    """


# ── Token helpers ─────────────────────────────────────────────────────────────


def _secret() -> str:
    """Return the signing secret; raise clearly if unset."""
    s = os.getenv(_GUEST_SECRET_ENV, "")
    if not s:
        raise RuntimeError(
            f"{_GUEST_SECRET_ENV} is not set. Generate one with: openssl rand -hex 32"
        )
    return s


def _invite_ttl() -> int:
    """Return invite TTL in seconds (env-configurable, capped at MAX)."""
    try:
        v = int(os.getenv(_INVITE_TTL_ENV, str(_DEFAULT_INVITE_TTL)))
    except (TypeError, ValueError):
        v = _DEFAULT_INVITE_TTL
    return min(v, _MAX_INVITE_TTL)


# ── Dataclasses ───────────────────────────────────────────────────────────────


@dataclass
class GuestToken:
    """Server-side object representing a validated guest invite.

    Not transmitted to the guest. Used within the same request to build
    the LiveKit participant JWT via ``build_livekit_token()``.
    """

    jti: str
    room: str
    identity: str  # "guest:<jti[:8]>"
    display: str  # display name to show in the room
    exp: float  # Unix timestamp matching the invite token's exp
    perms: list[str] = field(default_factory=lambda: list(_GUEST_PERMS))


# ── Invite issuer ─────────────────────────────────────────────────────────────


class InviteIssuer:
    """Operator-facing: create signed invite tokens + shareable URLs."""

    def __init__(
        self,
        *,
        secret: str | None = None,
        now_fn: Optional[object] = None,  # callable → float; injectable for tests
    ) -> None:
        self._secret = secret  # None → read from env at call time
        self._now: object = now_fn or time.time

    def _get_secret(self) -> str:
        return self._secret if self._secret is not None else _secret()

    def create_invite(
        self,
        room: str,
        *,
        display: str = "",
        ttl: int | None = None,
        issuer: str = "operator",
    ) -> dict:
        """Mint a signed invite token for ``room``.

        Args:
            room: LiveKit room name (must match when the guest joins).
            display: Optional display-name hint embedded in the token.
            ttl: Token lifetime in seconds (default from env; capped at MAX).
            issuer: FQID of the inviting operator (informational).

        Returns:
            {
                "invite_token": "<jwt>",
                "invite_url":   "<funnel_url>/join/<room>?invite=<jwt>",
                "jti":          "<hex>",
                "room":         "<room>",
                "expires_at":   <unix float>,
                "ttl":          <seconds>,
            }
        """
        try:
            import jwt as _jwt
        except ImportError as exc:
            raise RuntimeError("PyJWT not installed: pip install PyJWT") from exc

        if not room:
            raise ValueError("room is required")

        effective_ttl = min(ttl or _invite_ttl(), _MAX_INVITE_TTL)
        now = float(self._now())  # type: ignore[operator]
        exp = now + effective_ttl
        jti = secrets.token_hex(16)  # 128-bit random

        payload = {
            "jti": jti,
            "iss": issuer,
            "room": room,
            "display": display,
            "iat": int(now),
            "exp": int(exp),
            "tier": "invite",
        }
        token = _jwt.encode(payload, self._get_secret(), algorithm="HS256")

        funnel_base = os.getenv(_FUNNEL_URL_ENV, "").rstrip("/")
        invite_url = (
            f"{funnel_base}/join/{room}?invite={token}"
            if funnel_base
            else f"/join/{room}?invite={token}"
        )

        return {
            "invite_token": token,
            "invite_url": invite_url,
            "jti": jti,
            "room": room,
            "expires_at": exp,
            "ttl": effective_ttl,
        }


# ── Invite verifier ───────────────────────────────────────────────────────────


class InviteVerifier:
    """Guest-facing: validate an invite token and produce a ``GuestToken``."""

    def __init__(
        self,
        *,
        secret: str | None = None,
        now_fn: Optional[object] = None,
    ) -> None:
        self._secret = secret
        self._now: object = now_fn or time.time

    def _get_secret(self) -> str:
        return self._secret if self._secret is not None else _secret()

    def verify(
        self,
        invite_token: str,
        *,
        expected_room: str,
        display_name: str = "",
    ) -> GuestToken:
        """Verify ``invite_token`` and return a ``GuestToken`` on success.

        Raises:
            GuestJoinError: for any invalid/expired/revoked/mismatched token.
                Callers MUST map this to a generic HTTP 401 without exposing
                the detail message to the requester.
        """
        try:
            import jwt as _jwt
            from jwt.exceptions import PyJWTError
        except ImportError as exc:
            raise RuntimeError("PyJWT not installed: pip install PyJWT") from exc

        # Decode + verify signature + expiry atomically.
        try:
            payload = _jwt.decode(
                invite_token,
                self._get_secret(),
                algorithms=["HS256"],
                options={"require": ["jti", "exp", "iat", "room", "tier"]},
            )
        except PyJWTError as exc:
            raise GuestJoinError(f"invite decode failed: {exc}") from exc

        # Check tier claim.
        if payload.get("tier") != "invite":
            raise GuestJoinError("not an invite token")

        # Room-scope check.
        token_room = payload.get("room", "")
        if not token_room or token_room != expected_room:
            raise GuestJoinError(
                f"room mismatch: token room '{token_room}' != requested '{expected_room}'"
            )

        # Revocation check.
        jti = payload["jti"]
        if _is_revoked(jti):
            raise GuestJoinError(f"invite token {jti!r} has been revoked")

        # Build identity: server-assigned, guest cannot influence it.
        identity = f"guest:{jti[:8]}"

        # Display name: body > token hint > fallback.
        effective_display = (
            display_name.strip()[:40] or payload.get("display", "").strip()[:40] or "Guest"
        )

        # exp is already validated by PyJWT; extract as float for TTL calc.
        exp = float(payload["exp"])

        return GuestToken(
            jti=jti,
            room=token_room,
            identity=identity,
            display=effective_display,
            exp=exp,
        )


# ── LiveKit token builder ─────────────────────────────────────────────────────


def build_livekit_token(
    guest: GuestToken,
    *,
    livekit_api_key: str,
    livekit_api_secret: str,
    now_fn: Optional[object] = None,
    allow_screenshare: bool = False,
) -> str:
    """Mint a LiveKit participant JWT from a validated ``GuestToken``.

    Grants are restricted to the guest permission set (see §5 of design doc):
      - can_publish=True (audio + camera only by default; sources restricted)
      - can_subscribe=True
      - can_publish_data=True (chat lane)
      - can_publish_sources=["camera", "microphone"]  (no screen-share by default)
      - recorder=False

    Conference-call invites pass ``allow_screenshare=True`` to additionally grant
    the screenshare sources. In that mode the grant is sourced from the single
    conference factory (``roles.conf_grant_for(ConfRole.GUEST_CONF, ...)``), which
    structurally guarantees a conf guest still NEVER receives
    room_admin/room_record/room_destroy — only the publish-source set widens.

    The default (``allow_screenshare=False``) is unchanged and screenshare-stripped,
    so existing audio-space guest joins are byte-for-byte backward compatible.

    TTL is bounded to the remaining lifetime of the invite token.

    Raises:
        ImportError: if livekit-api is not installed.
        RuntimeError: on any LiveKit API error.
    """
    from datetime import timedelta

    from livekit import api  # soft dep — same pattern as livekit_routes.py

    now = float((now_fn or time.time)())  # type: ignore[operator]
    remaining_seconds = max(60, int(guest.exp - now))
    ttl = min(remaining_seconds, _MAX_INVITE_TTL)

    grant = api.VideoGrants(
        room_join=True,
        room=guest.room,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
        # Restrict sources so guests cannot screen-share by default.
        # livekit-api >= 0.5 supports can_publish_sources; guard for older builds.
    )
    # Optional source restriction — set via attribute to stay compatible with
    # older livekit-api versions that may not have this kwarg.
    if hasattr(grant, "can_publish_sources"):
        if allow_screenshare:
            # Route through the single conf grant factory so a conf guest's
            # source set (and its admin denial) come from one audited place.
            from skchat.spaces.roles import ConfRole, conf_grant_for

            conf = conf_grant_for(ConfRole.GUEST_CONF, guest.room)
            grant.can_publish_sources = conf.can_publish_sources
            # Belt-and-suspenders: the factory already forces these False for a
            # conf guest; never let them leak onto the LiveKit grant.
            grant.room_admin = conf.room_admin  # False
            if hasattr(grant, "room_record"):
                grant.room_record = conf.room_record  # False
        else:
            grant.can_publish_sources = ["camera", "microphone"]

    token = (
        api.AccessToken(livekit_api_key, livekit_api_secret)
        .with_identity(guest.identity)
        .with_name(guest.display)
        .with_grants(grant)
        .with_ttl(timedelta(seconds=ttl))
    )
    return token.to_jwt()


# ── Join page HTML ────────────────────────────────────────────────────────────


def guest_join_page_html(room: str, invite_token: str, error: str = "") -> str:
    """Return a minimal HTML join page for the given room + invite token.

    Served at GET /join/<room>?invite=<token>.  The form POSTs to /guest/join
    which returns JSON; a small inline script handles the redirect to livekit.html.
    The invite token is carried in a hidden form field so the browser does not
    need to re-read the URL after a reload.

    Security: ``room``, ``invite_token``, and ``error`` are HTML-escaped before
    insertion.
    """
    safe_room = html.escape(room)
    safe_token = html.escape(invite_token)
    safe_error = html.escape(error) if error else ""
    error_block = f'<p class="err">{safe_error}</p>' if safe_error else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Join {safe_room} · SKChat</title>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
  :root{{color-scheme:dark;--accent:#5a8dee;--bad:#f08a8a;}}
  *{{box-sizing:border-box;}}
  body{{font-family:ui-sans-serif,system-ui,sans-serif;background:#0b0d12;color:#e4e7ee;
       margin:0;padding:0;min-height:100vh;display:flex;align-items:center;justify-content:center;}}
  .card{{background:#11141b;border:1px solid #1f2330;border-radius:14px;padding:32px 36px;
         max-width:400px;width:100%;box-shadow:0 4px 32px rgba(0,0,0,.4);}}
  h1{{font-size:18px;margin:0 0 4px;font-weight:700;}}
  .sub{{font-size:13px;color:#8b94a7;margin-bottom:24px;}}
  label{{font-size:11px;color:#8b94a7;text-transform:uppercase;letter-spacing:.05em;display:block;margin-bottom:4px;}}
  input{{width:100%;background:#181b22;color:#e4e7ee;border:1px solid #2a2f3a;
         padding:9px 12px;border-radius:8px;font:inherit;font-size:14px;margin-bottom:16px;}}
  input:focus{{outline:none;border-color:var(--accent);}}
  button{{width:100%;background:var(--accent);border:1px solid var(--accent);color:#fff;
          padding:11px;border-radius:8px;font:inherit;font-size:14px;font-weight:600;
          cursor:pointer;transition:opacity .15s;}}
  button:disabled{{opacity:.45;cursor:not-allowed;}}
  .err{{color:var(--bad);font-size:13px;margin-bottom:12px;}}
  .agents{{font-size:12px;color:#7ee0b9;margin-bottom:20px;}}
</style>
</head>
<body>
<div class="card">
  <h1>Join <em>{safe_room}</em></h1>
  <p class="sub">You have been invited to a call</p>
  <p class="agents">Lumina and Opus are in this room</p>
  {error_block}
  <form id="f" onsubmit="join(event)">
    <label for="dn">Your name</label>
    <input id="dn" name="display_name" placeholder="e.g. Alice" maxlength="40" required autocomplete="name"/>
    <input type="hidden" name="room" value="{safe_room}"/>
    <input type="hidden" name="invite_token" value="{safe_token}"/>
    <button id="btn" type="submit">Join call</button>
  </form>
</div>
<script>
async function join(e) {{
  e.preventDefault();
  const btn = document.getElementById('btn');
  btn.disabled = true;
  btn.textContent = 'Joining…';
  const fd = new FormData(e.target);
  const body = Object.fromEntries(fd.entries());
  try {{
    const r = await fetch('/guest/join', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(body),
    }});
    if (!r.ok) {{
      const t = await r.text();
      btn.disabled = false;
      btn.textContent = 'Join call';
      document.getElementById('f').insertAdjacentHTML(
        'afterbegin', '<p class="err">Could not join: ' + t + '</p>');
      return;
    }}
    const data = await r.json();
    const params = new URLSearchParams({{
      room: data.room,
      identity: data.identity,
      token: data.lk_token,
    }});
    window.location.href = '/livekit/' + encodeURIComponent(data.room) + '?' + params.toString();
  }} catch (err) {{
    btn.disabled = false;
    btn.textContent = 'Join call';
    document.getElementById('f').insertAdjacentHTML(
      'afterbegin', '<p class="err">Network error — please retry.</p>');
  }}
}}
</script>
</body>
</html>"""


def join_chooser_html(room: str, invite_token: str) -> str:
    """Return the join *chooser* page (``static/join.html``) for ``room``.

    Served at GET /join/<room>?invite=<token>. One invite link, TWO identity
    branches (the invite secret authorizes ENTRY; identity choice is downstream):

      * GUEST     — the existing flow: name -> POST /guest/join -> LiveKit token.
      * SOVEREIGN — a capauth-signed FQID assertion -> POST /join/sovereign,
        whose minted token's identity is the PROVEN fqid (never caller-supplied).

    The static template carries ``{{ROOM}}`` / ``{{INVITE}}`` placeholders which
    are HTML-escaped here before substitution (same XSS posture as the legacy
    ``guest_join_page_html``). Falls back to that legacy guest-only page if the
    static asset is missing, so a broken deploy still serves a working guest join.
    """
    import pathlib

    safe_room = html.escape(room)
    safe_token = html.escape(invite_token)
    template_path = pathlib.Path(__file__).parent / "static" / "join.html"
    try:
        template = template_path.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - missing asset → degrade to guest page
        logger.warning("join.html not found at %s; serving guest-only page", template_path)
        return guest_join_page_html(room, invite_token)
    return template.replace("{{ROOM}}", safe_room).replace("{{INVITE}}", safe_token)


# ── FastAPI route registration (wired from webui.py) ──────────────────────────────


def register_guest_routes(app: object) -> None:  # app: FastAPI
    """Register guest join + invite endpoints on the FastAPI app.

    Call this after ``register_livekit_routes(app)`` in webui.py or the app
    factory.  The import is guarded so skchat works even when FastAPI is not
    installed (e.g. pure CLI usage).

    Wired from ``src/skchat/webui.py`` (after the spaces/glossa route block).
    ``/guest/invite`` + ``/guest/revoke`` are operator-gated (``_require_operator``);
    ``/join/{room}`` + ``/guest/join`` stay public (invite-JWT-gated).

    Routes registered:
        POST   /guest/invite           — operator creates a shareable invite
        GET    /join/{room}            — serve the join chooser HTML page
        POST   /guest/join             — validate invite → return LiveKit token
        DELETE /guest/revoke/{jti}     — operator revokes a live invite
    """
    try:
        from fastapi import FastAPI, HTTPException, Request  # noqa: F401
        from fastapi.responses import HTMLResponse, JSONResponse
    except ImportError:
        logger.warning("FastAPI not available; guest routes not registered")
        return

    import os

    from fastapi import HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse

    livekit_api_key = os.getenv("SKCHAT_LIVEKIT_API_KEY", "")
    livekit_api_secret = os.getenv("SKCHAT_LIVEKIT_API_SECRET", "")

    issuer = InviteIssuer()
    verifier = InviteVerifier()

    @app.post("/guest/invite")  # type: ignore[attr-defined]
    async def guest_invite(request: Request) -> JSONResponse:
        """Operator-only: create a shareable invite link for a room.

        Body (JSON):
            room:     LiveKit room name (required)
            display:  Optional display-name hint for the invite page
            ttl:      Token lifetime in seconds (optional; default from env)

        Operator-auth: gated by ``_require_operator`` -- a shared bearer token
        (``SKCHAT_GUEST_OPERATOR_TOKEN``) when set, else loopback/tailnet-only.
        Anonymous/public callers are rejected (401/403) before any invite is
        minted.
        """
        _require_operator(request)
        try:
            body = await request.json()
        except Exception as exc:
            logger.debug("guest request body not valid JSON (%s: %s)", type(exc).__name__, exc)
            body = {}
        room = (body.get("room") or "").strip()
        if not room:
            raise HTTPException(status_code=400, detail="room is required")
        display = body.get("display") or ""
        ttl_raw = body.get("ttl")
        ttl: int | None = None
        if ttl_raw is not None:
            try:
                ttl = int(ttl_raw)
            except (TypeError, ValueError):
                pass

        try:
            result = issuer.create_invite(room, display=display, ttl=ttl)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        return JSONResponse(result)

    @app.get("/join/{room}", response_class=HTMLResponse)  # type: ignore[attr-defined]
    async def guest_join_page(room: str, request: Request) -> HTMLResponse:
        """Public (Funnel): serve the join *chooser* landing page.

        One invite link offers TWO identity branches (the invite secret
        authorizes ENTRY; identity choice is downstream):
          * "Join as Chef (sign in)"  -> capauth assertion -> POST /join/sovereign
          * "Join as guest (a name)"  -> POST /guest/join (unchanged guest flow)

        Query params:
            invite: the signed invite token (required)
        """
        invite_token = request.query_params.get("invite", "")
        if not invite_token:
            return HTMLResponse(
                "<h2>Invalid invite link</h2><p>The invite parameter is missing.</p>",
                status_code=400,
            )
        return HTMLResponse(join_chooser_html(room, invite_token))

    @app.post("/guest/join")  # type: ignore[attr-defined]
    async def guest_join(request: Request) -> JSONResponse:
        """Public (Funnel): validate an invite and return a LiveKit token.

        Body (JSON):
            room:          Room name (must match invite token's room claim)
            invite_token:  The signed invite JWT
            display_name:  Guest's chosen display name (required by the join page)
        """
        try:
            body = await request.json()
        except Exception as exc:
            logger.debug("guest request body not valid JSON (%s: %s)", type(exc).__name__, exc)
            body = {}

        room = (body.get("room") or "").strip()
        invite_token = (body.get("invite_token") or "").strip()
        display_name = (body.get("display_name") or "").strip()[:40]

        if not room or not invite_token:
            raise HTTPException(status_code=400, detail="room and invite_token are required")

        try:
            guest = verifier.verify(invite_token, expected_room=room, display_name=display_name)
        except GuestJoinError as exc:
            logger.info("guest join rejected: %s", exc)
            raise HTTPException(status_code=401, detail="invalid or expired invite") from exc

        if not livekit_api_key or not livekit_api_secret:
            raise HTTPException(
                status_code=503,
                detail="livekit not configured: set SKCHAT_LIVEKIT_API_KEY/SECRET",
            )

        try:
            lk_token = build_livekit_token(
                guest,
                livekit_api_key=livekit_api_key,
                livekit_api_secret=livekit_api_secret,
            )
        except ImportError:
            raise HTTPException(
                status_code=503, detail="livekit-api not installed: pip install livekit-api"
            )
        except Exception as exc:
            logger.exception("guest livekit token mint failed")
            raise HTTPException(status_code=500, detail=f"token mint failed: {exc}") from exc

        # Public-aware SFU URL: a guest arriving via Funnel (public Host /
        # X-Forwarded-Host) gets the public wss URL; a tailnet caller keeps the
        # tailnet URL. Falls back to the tailnet default when the helper or
        # FastAPI is unavailable. See livekit_routes.public_aware_livekit_url.
        try:
            from skchat.livekit_routes import public_aware_livekit_url

            livekit_url = public_aware_livekit_url(request)
        except Exception:  # pragma: no cover - defensive fallback
            livekit_url = os.getenv("SKCHAT_LIVEKIT_URL", "ws://skworld-100:7880")
        ttl_remaining = max(0, int(guest.exp - time.time()))

        return JSONResponse(
            {
                "room": guest.room,
                "identity": guest.identity,
                "display": guest.display,
                "lk_token": lk_token,
                "lk_url": livekit_url,
                "expires_at": guest.exp,
                "ttl_seconds": ttl_remaining,
            }
        )

    @app.delete("/guest/revoke/{jti}")  # type: ignore[attr-defined]
    async def guest_revoke(jti: str, request: Request) -> JSONResponse:
        """Operator-only: revoke a live invite token by JTI.

        Operator-auth: gated by ``_require_operator`` (shared bearer token when
        ``SKCHAT_GUEST_OPERATOR_TOKEN`` is set, else loopback/tailnet-only), so
        anonymous/public callers cannot revoke invites.
        """
        _require_operator(request)
        revoke_invite(jti)
        logger.info("guest invite revoked: jti=%s", jti)
        return JSONResponse({"ok": True, "revoked_jti": jti})
