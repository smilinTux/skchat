"""Iframe-friendly, module-scoped EMBED tokens for the SKWorld shell panes.

Problem this solves
-------------------
The shell's "Board" (``/skdashboard``) and "OS" (``/skos``) panes are Grade B
IFRAMES. To close the public-leak (Fable review A1/A4) those two reverse proxies
now require operator/dataplane auth (:func:`enforce_dataplane_auth`). But an
iframe cannot set an ``Authorization`` header on the document request the browser
makes for its ``src``, so the pane can only 401. skcode does not have this problem
(skcode-hostd runs its own deny-all gate and its public client shell is safe to
expose); skdashboard and skos are NOT safe to expose publicly.

The fix (no public leak, no new crypto)
--------------------------------------
The AUTHENTICATED app mints a SHORT-LIVED, module-SCOPED, READ-ONLY embed token
from ``POST /api/v1/embed-token`` (auth required) and appends it to the iframe
``src`` as ``?embed_token=...``. The ``/skdashboard`` and ``/skos`` proxies accept
EITHER a valid ``Authorization`` credential OR a valid ``embed_token`` scoped to
that exact module id. An unauthenticated request carrying no/invalid token still
401s, so the leak stays closed.

This module deliberately REUSES the exact HS256 PyJWT machinery
:mod:`skchat.operator_auth` already uses for operator-session tokens (same
library, same algorithm, its own tier + its own domain-separated signing key), so
no new crypto is introduced. The embed token is distinguished from an operator
session by a dedicated ``tier`` claim AND a distinct signing key, exactly the
separation ``operator_auth`` documents between operator and guest tokens.

Key material
------------
The signing key is domain-separated from the operator-session secret so an embed
token and an operator session can never be confused or cross-replayed, WITHOUT
requiring a new secret to be provisioned on the box:

  * If ``SKCHAT_EMBED_TOKEN_SECRET`` is set, it is used verbatim.
  * Otherwise the key is derived from ``SKCHAT_OPERATOR_TOKEN_SECRET`` via a
    fixed domain tag (``sha256("skchat-embed-token-v1|" + operator_secret)``).
    This is ordinary key separation (a keyed hash), not a new cipher.

Scope + lifetime
----------------
* ``tier`` is always ``"embed-token"``.
* ``module`` is the exact proxy module id the token authorizes (``skdashboard``
  or ``skos``); a token minted for one module never authorizes another.
* ``mode`` is always ``"ro"`` (read-only); the proxy refuses any non-GET/HEAD
  request that presents only an embed token.
* TTL defaults to 120s and is capped at 600s. Short by design: the token only has
  to survive the initial iframe navigation, after which the proxy hands the pane a
  path-scoped, HttpOnly cookie carrying the same token so subresource requests
  (the pane's own ``fetch``/asset loads, which cannot re-attach the query param)
  stay authorized for the rest of the token's short life.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from dataclasses import dataclass

import jwt  # PyJWT, already a dependency (see operator_auth.py / guest_groups.py)
from fastapi import Request

logger = logging.getLogger("skchat.embed_auth")

#: Fixed tier for every embed token. Distinguishes it from an operator-session
#: (``operator-session``) or guest token, so one credential type can never be
#: replayed as another even before the distinct signing key is considered.
_TIER = "embed-token"

#: Read-only marker carried by every embed token. The proxy refuses any non-GET
#: request that authorizes only via an embed token.
_MODE_RO = "ro"

#: Default / maximum embed-token lifetime (seconds). Short: it only has to outlive
#: the initial iframe navigation before the scoped cookie takes over.
DEFAULT_TTL = 120
MAX_TTL = 600

#: The proxy module ids an embed token may be scoped to. skchat/skcode are NOT
#: here: skchat is the shell's own origin and skcode runs its own gate + safe
#: public client, so neither needs (or may mint) an embed token.
EMBED_MODULES = frozenset({"skdashboard", "skos"})

#: Opt-in flag for the MINT endpoint (POST /api/v1/embed-token). Default OFF so
#: the route is inert (404) and the app is byte-identical to before it existed,
#: mirroring the audience-token mint gate. The PROXY acceptance of a valid embed
#: token is intentionally NOT gated on this flag: a token can only exist if an
#: authenticated caller minted it while the flag was on, so accepting a validly
#: signed, unexpired, correctly-scoped token never widens exposure.
EMBED_MINT_ENV_FLAG = "SKCHAT_EMBED_TOKENS"

#: Optional explicit signing secret. When unset the key is derived (below) from
#: the operator-session secret, so no new secret has to be provisioned.
_EMBED_SECRET_ENV = "SKCHAT_EMBED_TOKEN_SECRET"
_OPERATOR_SECRET_ENV = "SKCHAT_OPERATOR_TOKEN_SECRET"

#: Domain tag for deriving the embed key from the operator secret. Any change to
#: this string rotates every outstanding embed token (they simply stop verifying),
#: which is acceptable given their <=10 minute lifetime.
_DERIVE_TAG = b"skchat-embed-token-v1|"

_TRUTHY = {"1", "true", "yes", "on"}


class EmbedAuthError(Exception):
    """Raised when an embed token is missing, malformed, expired, or wrong-scope."""


@dataclass
class EmbedToken:
    jti: str
    module: str
    exp: int


def embed_tokens_enabled() -> bool:
    """Return True iff the embed-token MINT endpoint is switched on (default OFF).

    Read at call time (like :func:`dataplane_auth.dataplane_auth_enabled`) so an
    operator editing the unit's ``Environment=`` line, or a test, can flip it
    without a reimport. Default OFF: the mint route is inert (404).
    """
    return os.getenv(EMBED_MINT_ENV_FLAG, "").strip().lower() in _TRUTHY


def _secret() -> str:
    """Resolve the embed-token signing key (domain-separated from operator).

    Prefers an explicit ``SKCHAT_EMBED_TOKEN_SECRET``; otherwise derives a
    distinct key from ``SKCHAT_OPERATOR_TOKEN_SECRET`` so embed tokens and
    operator sessions never share signing material. Raises when neither is set
    (fail closed: nothing can be minted or verified without a key).
    """
    explicit = os.environ.get(_EMBED_SECRET_ENV, "")
    if explicit:
        return explicit
    operator = os.environ.get(_OPERATOR_SECRET_ENV, "")
    if not operator:
        raise EmbedAuthError(
            f"neither {_EMBED_SECRET_ENV} nor {_OPERATOR_SECRET_ENV} is set"
        )
    return hashlib.sha256(_DERIVE_TAG + operator.encode("utf-8")).hexdigest()


def cookie_name(module: str) -> str:
    """The path-scoped cookie name the proxy sets so subresource loads stay authed.

    One cookie per module (``skc_embed_skdashboard`` / ``skc_embed_skos``), each
    scoped to its own proxy path so a token for one module is never sent to
    another.
    """
    return f"skc_embed_{module}"


def cookie_path(module: str) -> str:
    """The ``Path`` the embed cookie is scoped to (the module's proxy prefix)."""
    return f"/{module}"


def mint_embed_token(module: str, *, ttl: int | None = None) -> tuple[str, int]:
    """Mint a short-lived, module-scoped, read-only embed token.

    Args:
        module: The proxy module id to authorize. MUST be in :data:`EMBED_MODULES`.
        ttl: Lifetime in seconds; defaults to :data:`DEFAULT_TTL`, capped at
            :data:`MAX_TTL`.

    Returns:
        ``(token, expires_at_epoch)``.

    Raises:
        EmbedAuthError: for an unknown module or a missing signing key.
    """
    if module not in EMBED_MODULES:
        raise EmbedAuthError(f"unknown embed module: {module!r}")
    now = int(time.time())
    ttl = DEFAULT_TTL if ttl is None else max(1, min(int(ttl), MAX_TTL))
    exp = now + ttl
    claims = {
        "jti": uuid.uuid4().hex,
        "tier": _TIER,
        "module": module,
        "mode": _MODE_RO,
        "iat": now,
        "exp": exp,
    }
    token = jwt.encode(claims, _secret(), algorithm="HS256")
    return token, exp


def verify_embed_token(token: str, module: str) -> EmbedToken:
    """Verify an embed token is valid AND scoped to ``module``. Raises on failure.

    Checks, in order: a well-formed HS256 signature over our key, the required
    claim set, the fixed ``embed-token`` tier, the read-only mode, non-expiry
    (PyJWT enforces ``exp``), and that the ``module`` claim equals the module the
    caller is authorizing. A token minted for a different module (foreign scope)
    is rejected here.
    """
    if not token:
        raise EmbedAuthError("empty embed token")
    try:
        claims = jwt.decode(
            token,
            _secret(),
            algorithms=["HS256"],
            options={"require": ["jti", "tier", "module", "mode", "iat", "exp"]},
        )
    except jwt.PyJWTError as e:
        raise EmbedAuthError(f"invalid embed token: {e}") from e
    if claims.get("tier") != _TIER:
        raise EmbedAuthError("wrong tier")
    if claims.get("mode") != _MODE_RO:
        raise EmbedAuthError("wrong mode")
    if claims.get("module") != module:
        raise EmbedAuthError(
            f"embed token scoped to {claims.get('module')!r}, not {module!r}"
        )
    return EmbedToken(jti=claims["jti"], module=claims["module"], exp=claims["exp"])


def _token_from_request(request: Request, module: str) -> str | None:
    """Pull an embed token off a request: ``?embed_token=`` first, then the cookie.

    The query param carries the token on the initial iframe navigation; the
    path-scoped cookie (set by the proxy on that first response) carries it on the
    pane's subsequent subresource loads, which cannot re-attach the query param.
    """
    q = (request.query_params.get("embed_token") or "").strip()
    if q:
        return q
    c = (request.cookies.get(cookie_name(module)) or "").strip()
    return c or None


def request_embed_ok(request: Request, module: str) -> bool:
    """Return True iff the request carries a valid embed token scoped to ``module``.

    Never raises: a missing/invalid/expired/foreign-scope token is simply False,
    so the caller can fall through to the operator-auth gate.
    """
    token = _token_from_request(request, module)
    if not token:
        return False
    try:
        verify_embed_token(token, module)
        return True
    except EmbedAuthError:
        return False
    except Exception:  # pragma: no cover - defensive: any key/parse error denies
        logger.info("embed token rejected", exc_info=True)
        return False


def presented_via_query(request: Request) -> bool:
    """True iff the (initial) request presented the token as a query param.

    The proxy uses this to decide when to (re)issue the path-scoped cookie: it
    sets the cookie on the first navigation (query-param path) so the pane's later
    subresource loads authorize via the cookie.
    """
    return bool((request.query_params.get("embed_token") or "").strip())


__all__ = [
    "EMBED_MODULES",
    "EMBED_MINT_ENV_FLAG",
    "DEFAULT_TTL",
    "MAX_TTL",
    "EmbedAuthError",
    "EmbedToken",
    "embed_tokens_enabled",
    "mint_embed_token",
    "verify_embed_token",
    "request_embed_ok",
    "presented_via_query",
    "cookie_name",
    "cookie_path",
]
