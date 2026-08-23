"""Parallel operator-audience token issuance (CR-3.4 AC1 / Phase 1).

Mints a capauth audience token (``audience=skchat``) whose subject is
``device:<device_fp>``, ALONGSIDE the HS256 operator session, at the device
challenge-response handshake (``POST /api/v1/auth/session``). This is ADDITIVE and
PARALLEL: the HS256 session stays the primary credential the client uses; the
audience token is issued only when ``SKCHAT_OPERATOR_AUDIENCE_ISSUE`` is on, and
any mint failure is NON-FATAL (logged, ``None`` returned) so a capauth keyring
problem can never lock the operator seat out during the parallel phases.

The subject is derived server-side from the verified device fingerprint (the
session route has already checked a fresh device signature over the challenge
nonce), never from request input, so no anti-forgery contract is weakened.

Kept in its own module so the pure-HS256 mint/verify primitive (now
``capauth.pairing.operator_session``, moved out of skchat's former
``operator_auth.py`` in coord ``3731ae06``) stays independently retireable in
the final retirement phase (Phase 5) without disturbing the audience path.
"""

from __future__ import annotations

import base64
import logging
import os
import threading
import time
from typing import Optional

from .dataplane_auth import SKCHAT_AUDIENCE, operator_subject

logger = logging.getLogger("skchat.operator_audience")

#: Per-fingerprint reuse cache for the issued audience token:
#: ``device_fp -> (wire, expires_at_iso, expires_at_epoch)``. Without it,
#: :func:`issue_operator_audience` minted a fresh token on EVERY session handshake
#: and each mint stores a file, which flooded the token store (38k files). The
#: token is reused until shortly before expiry, mirroring the shadow twin cache.
_operator_audience_cache: dict[str, tuple[str, str, float]] = {}
_operator_audience_lock = threading.Lock()

#: Re-mint this many seconds before expiry (same rhythm as the shadow twin's
#: ``_SHADOW_REFRESH_SKEW``), so a token is never handed out about to expire.
_AUDIENCE_REFRESH_SKEW = 300

#: Opportunistic store GC: prune expired token files after a real mint, at most
#: once per this interval so a hot handshake path never re-scans the store.
_GC_MIN_INTERVAL = 3600.0
_last_gc = [0.0]

_TRUTHY = {"1", "true", "yes", "on"}

#: Opt-in flag: dual-mint an operator-audience token at the session handshake.
#: Default OFF, read at call time, so the handshake is byte-identical until an
#: operator enables it on the webui unit.
OPERATOR_AUDIENCE_ISSUE_FLAG = "SKCHAT_OPERATOR_AUDIENCE_ISSUE"

#: Seat issuer policy echoed to the client (CR-3.4 PR4 / P6). Read at call time
#: so a unit env flip drives the client with no app rebuild. `hs256` (default):
#: client attaches the HS256 session. `prefer-audience`: client attaches the
#: audience token, HS256 auto-fallback on 401/403. `audience-only`: HS256 no
#: longer minted (Phase 5). Any unknown value normalizes to the safe hs256
#: default, so a typo can never silently disable operator auth.
OPERATOR_ISSUER_POLICY_FLAG = "SKCHAT_OPERATOR_ISSUER_POLICY"

_VALID_ISSUER_POLICIES = frozenset({"hs256", "prefer-audience", "audience-only"})

_DEFAULT_ISSUER_POLICY = "hs256"


def operator_issuer_policy() -> str:
    """Return the operator seat's issuer policy from ``SKCHAT_OPERATOR_ISSUER_POLICY``.

    Read at call time (like every other gate on this plane). An unset, empty, or
    unrecognized value returns the safe default ``hs256`` so a misconfiguration
    can never leave the client without a working credential.
    """
    value = os.getenv(OPERATOR_ISSUER_POLICY_FLAG, "").strip().lower()
    return value if value in _VALID_ISSUER_POLICIES else _DEFAULT_ISSUER_POLICY


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
    """Mint an operator-audience capauth token for ``device:<device_fp>``.

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
        # An operator-audience token is self-contained (verified by signature,
        # never looked up in the store), so it is never persisted: writing one
        # file per mint is exactly what flooded the store (card e793b6bc). The
        # reuse cache already cut the mint rate; this stops the write at source.
        store=False,
    )


def wire_form(token) -> str:
    """Encode a capauth ``SignedToken`` into the credential wire form.

    base64url of ``capauth.export_token`` JSON (whitespace-free so it rides in an
    ``Authorization`` header), the exact form the dataplane validator accepts and
    mirrors :func:`skchat.webui.audience_token_mint`.
    """
    from capauth import export_token

    return (
        base64.urlsafe_b64encode(export_token(token).encode("utf-8")).decode("ascii").rstrip("=")
    )


def _gc_token_store() -> None:
    """Prune expired token files from the store, at most once per interval.

    The audience mint path writes one file per token via capauth ``_store_token``;
    the flood was those files never being reaped. Rate-limited so the hot handshake
    path never re-scans the store, and fully best-effort: any error is swallowed
    (GC is hygiene, never on the critical path).
    """
    now = time.time()
    if now - _last_gc[0] < _GC_MIN_INTERVAL:
        return
    _last_gc[0] = now
    try:
        from capauth import resolve_capauth_home
        from capauth.tokens import prune_expired_tokens

        removed = prune_expired_tokens(resolve_capauth_home())
        if removed:
            logger.info("pruned %d expired token files from the store", removed)
    except Exception:
        logger.debug("token-store GC skipped (non-fatal)", exc_info=True)


def issue_operator_audience(device_fp: str) -> Optional[dict]:
    """Best-effort parallel mint for the session handshake, with per-fp reuse.

    Returns ``{"audience_token": <wire>, "audience_expires_at": <iso>}`` when the
    flag is on and a token is available, else ``None``. A still-valid cached token
    for this fingerprint is REUSED (no new mint, no new stored file) until it is
    within :data:`_AUDIENCE_REFRESH_SKEW` of expiry; only then is a fresh one
    minted. NON-FATAL: the flag being off, or ANY mint/keyring/signing error,
    returns ``None`` (logged) so the caller still returns the HS256 session and the
    seat can never be locked out by the audience path during the parallel phases.
    """
    if not operator_audience_issue_enabled():
        return None

    now = time.time()
    with _operator_audience_lock:
        cached = _operator_audience_cache.get(device_fp)
        if cached is not None and cached[2] - _AUDIENCE_REFRESH_SKEW > now:
            return {"audience_token": cached[0], "audience_expires_at": cached[1]}

    try:
        token = mint_operator_audience_token(device_fp)
        wire = wire_form(token)
        exp = token.payload.expires_at
        exp_iso = exp.isoformat() if hasattr(exp, "isoformat") else exp
        exp_epoch = (
            exp.timestamp() if hasattr(exp, "timestamp") else now + AUDIENCE_TTL_HOURS * 3600
        )
    except Exception:
        logger.warning(
            "operator-audience mint failed for %s (non-fatal, HS256 session unaffected)",
            device_fp,
            exc_info=True,
        )
        return None

    with _operator_audience_lock:
        _operator_audience_cache[device_fp] = (wire, exp_iso, exp_epoch)
    _gc_token_store()
    return {"audience_token": wire, "audience_expires_at": exp_iso}


__all__ = [
    "OPERATOR_AUDIENCE_ISSUE_FLAG",
    "OPERATOR_ISSUER_POLICY_FLAG",
    "AUDIENCE_TTL_HOURS",
    "operator_audience_issue_enabled",
    "operator_issuer_policy",
    "mint_operator_audience_token",
    "wire_form",
    "issue_operator_audience",
]
