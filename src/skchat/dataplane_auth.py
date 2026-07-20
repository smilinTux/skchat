"""Fail-closed CapAuth gate for the chat data plane (P0.5 / SEAM 7).

The chat data-plane endpoints — ``POST /api/send``, ``POST /api/v1/prekey`` and
``GET /api/v1/inbox`` — historically shipped with **no** authentication: anyone
who can reach the port can send as the operator, publish a prekey, or read the
inbox. This module adds an **opt-in** CapAuth gate that mirrors the signature-
verification the call/signaling routes already do (a request must carry a valid
capauth credential or it is refused).

The gate is switched by the ``SKCHAT_DATAPLANE_AUTH`` env flag and defaults
**OFF** so the live app is not locked out before it starts signing its requests:

  * flag OFF (default) — endpoints behave exactly as before; the validator is
    never consulted and no credential is required.
  * flag ON — a missing **or** invalid capauth credential yields ``401``; a
    valid one passes through unchanged.

Validation is delegated to :class:`CapAuthValidator`, which is *injectable*
(``set_validator`` / ``get_validator``) so tests exercise the gate with capauth
fully mocked and production can wire the real verifier. The default validator
lazy-imports capauth (mirroring ``spaces/federation/assertion.py``) and **fails
closed** — any error resolving or running the verifier denies the request.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import HTTPException, Request

logger = logging.getLogger("skchat.dataplane_auth")

ENV_FLAG = "SKCHAT_DATAPLANE_AUTH"
_TRUTHY = {"1", "true", "yes", "on"}


def dataplane_auth_enabled() -> bool:
    """Return True iff the fail-closed data-plane CapAuth gate is switched on.

    Reads ``SKCHAT_DATAPLANE_AUTH`` at call time (not import time) so an
    operator editing the unit's ``Environment=`` line — or a test — can flip it
    without a reimport. Default OFF: absent / blank / anything not in the truthy
    set leaves the plane unauthenticated, exactly as before this gate existed.
    """
    return os.getenv(ENV_FLAG, "").strip().lower() in _TRUTHY


class CapAuthValidator:
    """Verify a capauth credential presented on a data-plane request.

    Thin delegate to the canonical capauth verifier, lazy-imported so this
    module loads even where capauth isn't installed (the same contract as
    ``spaces/federation/assertion.py``). ``validate`` returns True **only** for a
    credential capauth affirms; it **fails closed** (returns False) on a missing
    credential, a verification failure, or any error resolving the backend.

    Accepts an operator-session JWT (Bearer) or base64url-encoded {"claim", "sig"}
    OpenPGP FQID assertion, tried in that order. The OpenPGP form is verified
    through :func:`assertion.verify_signed`.
    """

    def validate(self, token: str) -> bool:
        if not token:
            return False
        try:
            return _verify_capauth_credential(token)
        except Exception:
            # Fail closed: any verifier/parse/backend error denies the request.
            logger.info("capauth credential rejected", exc_info=True)
            return False


def _verify_capauth_credential(token: str) -> bool:
    """Verify a base64url ``{claim, sig}`` capauth assertion. Raises on failure.

    Delegates to the in-repo, capauth-backed ``assertion.verify_signed`` (which
    checks the signature, freshness and the FQID->pubkey pin, raising on any
    problem). Lazy-imported so importing this module never drags in capauth.
    """
    # Operator-session JWT (the app's Bearer credential). Try this first;
    # fall through to the OpenPGP assertion path for daemon/agent callers.
    try:
        from .operator_auth import OperatorAuthError, verify_operator_session
        verify_operator_session(token)
        return True
    except OperatorAuthError:
        pass
    except Exception:
        logger.debug("operator-session credential check errored, falling through", exc_info=True)

    import base64
    import json

    from .spaces.federation.assertion import verify_signed

    padded = token + "=" * (-len(token) % 4)
    signed = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    if not isinstance(signed, dict) or "claim" not in signed or "sig" not in signed:
        return False
    verify_signed(signed)  # raises on bad signature / stale / unknown key
    return True


# --------------------------------------------------------------------------- #
# Injectable validator singleton
# --------------------------------------------------------------------------- #
_validator: Optional[CapAuthValidator] = None


def get_validator() -> CapAuthValidator:
    """Return the process validator, creating the default on first use."""
    global _validator
    if _validator is None:
        _validator = CapAuthValidator()
    return _validator


def set_validator(validator: Optional[CapAuthValidator]) -> None:
    """Override the process validator (tests inject a mock; ``None`` resets)."""
    global _validator
    _validator = validator


def _extract_credential(request: Request) -> Optional[str]:
    """Pull the capauth credential off a request, or None if absent.

    Accepts ``Authorization: CapAuth <token>`` / ``Bearer <token>`` (or a bare
    ``Authorization: <token>``) and falls back to the ``X-CapAuth-Token`` header.
    """
    auth = (request.headers.get("authorization") or "").strip()
    if auth:
        parts = auth.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in ("capauth", "bearer"):
            return parts[1].strip() or None
        if len(parts) == 1:
            return parts[0].strip() or None
    return (request.headers.get("x-capauth-token") or "").strip() or None


def enforce_dataplane_auth(request: Request) -> None:
    """Fail-closed CapAuth gate for a single data-plane request.

    No-op when the flag is off. When on, a missing **or** invalid capauth
    credential raises ``HTTPException(401)``; a valid one returns (the request
    proceeds unchanged).
    """
    if not dataplane_auth_enabled():
        return
    token = _extract_credential(request)
    if not token or not get_validator().validate(token):
        raise HTTPException(status_code=401, detail="capauth authentication required")


async def require_dataplane_auth(request: Request) -> None:
    """FastAPI dependency form of :func:`enforce_dataplane_auth`.

    Wire onto a protected route with ``Depends(require_dataplane_auth)``.
    """
    enforce_dataplane_auth(request)
