"""Parallel operator-audience token issuance (CR-3.4 AC1 / Phase 1).

Mints a capauth audience token (``audience=skchat``) whose subject is
``operator:<device_fp>``, ALONGSIDE the HS256 operator session, at the device
challenge-response handshake (``POST /api/v1/auth/session``). This is ADDITIVE and
PARALLEL: the HS256 session stays the primary credential the client uses; the
audience token is issued only when ``SKCHAT_OPERATOR_AUDIENCE_ISSUE`` is on, and
any mint failure is NON-FATAL (logged, ``None`` returned) so a capauth keyring
problem can never lock the operator seat out during the parallel phases.

The subject is derived server-side from the verified device fingerprint (the
session route has already checked a fresh device signature over the challenge
nonce), never from request input, so no anti-forgery contract is weakened.

Kept in its own module so ``operator_auth.py`` (pure HS256) stays deletable in the
final retirement phase (Phase 5) without disturbing the audience path.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Optional

from .dataplane_auth import SKCHAT_AUDIENCE, operator_subject

logger = logging.getLogger("skchat.operator_audience")

_TRUTHY = {"1", "true", "yes", "on"}

#: Opt-in flag: dual-mint an operator-audience token at the session handshake.
#: Default OFF, read at call time, so the handshake is byte-identical until an
#: operator enables it on the webui unit.
OPERATOR_AUDIENCE_ISSUE_FLAG = "SKCHAT_OPERATOR_AUDIENCE_ISSUE"

#: Audience-token TTL in hours. Matches ``operator_auth._DEFAULT_TTL`` (12h) so both
#: credentials share the seat's re-auth rhythm; refresh is the SAME device
#: challenge-response handshake (the device key stays the root credential).
AUDIENCE_TTL_HOURS = 12

#: The audience-token tier tag. Observability + future revocation tooling ONLY;
#: nothing authorizes off metadata (the subject string is the single PDP fact).
_OPERATOR_TIER = "operator-session"

#: Fallback scopes if capauth's AUDIENCE_SCOPES lacks a skchat entry (it does not,
#: but stay resilient rather than raising in the mint path).
_FALLBACK_SCOPES = ["chat.read", "chat.send", "calls.join", "spaces.join"]


def operator_audience_issue_enabled() -> bool:
    """Return True iff parallel operator-audience issuance is switched on."""
    return os.getenv(OPERATOR_AUDIENCE_ISSUE_FLAG, "").strip().lower() in _TRUTHY


def mint_operator_audience_token(device_fp: str):
    """Mint an operator-audience capauth token for ``operator:<device_fp>``.

    Signs with THIS daemon's capauth key (``resolve_capauth_home``) exactly as the
    daemon-self mint route does, and returns the capauth ``SignedToken``. Raises on
    any capauth/keyring error; the best-effort wrapper :func:`issue_operator_audience`
    catches that so the handshake stays non-fatal.
    """
    from capauth import mint_audience_token, resolve_capauth_home
    from capauth.tokens import AUDIENCE_SCOPES

    scopes = list(AUDIENCE_SCOPES.get(SKCHAT_AUDIENCE, _FALLBACK_SCOPES))
    return mint_audience_token(
        home=resolve_capauth_home(),
        subject=operator_subject(device_fp),
        audience=SKCHAT_AUDIENCE,
        scopes=scopes,
        ttl_hours=AUDIENCE_TTL_HOURS,
        metadata={"tier": _OPERATOR_TIER, "device_fp": device_fp},
    )


def wire_form(token) -> str:
    """Encode a capauth ``SignedToken`` into the credential wire form.

    base64url of ``capauth.export_token`` JSON (whitespace-free so it rides in an
    ``Authorization`` header), the exact form the dataplane validator accepts and
    mirrors :func:`skchat.webui.audience_token_mint`.
    """
    from capauth import export_token

    return (
        base64.urlsafe_b64encode(export_token(token).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )


def issue_operator_audience(device_fp: str) -> Optional[dict]:
    """Best-effort parallel mint for the session handshake.

    Returns ``{"audience_token": <wire>, "audience_expires_at": <iso>}`` when the
    flag is on and the mint succeeds, else ``None``. NON-FATAL: the flag being off, or
    ANY mint/keyring/signing error, returns ``None`` (logged) so the caller still
    returns the HS256 session and the seat can never be locked out by the audience
    path during the parallel phases.
    """
    if not operator_audience_issue_enabled():
        return None
    try:
        token = mint_operator_audience_token(device_fp)
        exp = token.payload.expires_at
        return {
            "audience_token": wire_form(token),
            "audience_expires_at": exp.isoformat() if hasattr(exp, "isoformat") else exp,
        }
    except Exception:
        logger.warning(
            "operator-audience mint failed for %s (non-fatal, HS256 session unaffected)",
            device_fp,
            exc_info=True,
        )
        return None


__all__ = [
    "OPERATOR_AUDIENCE_ISSUE_FLAG",
    "AUDIENCE_TTL_HOURS",
    "operator_audience_issue_enabled",
    "mint_operator_audience_token",
    "wire_form",
    "issue_operator_audience",
]
