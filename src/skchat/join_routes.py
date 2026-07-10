"""Local (non-federation) sovereign conference join.

Today, capauth-proven token minting only exists behind the federation route
``/sfu/get`` (``spaces.federation.authd.authorize``). That path is built for
*remote* peers: it applies a per-FQID TrustPolicy and caps remotes at
speaker/listener in an *audio* Space.

This module adds the LOCAL analog for *conference video* calls: a client that
holds a capauth key proves ownership of its own FQID by signing an
``Assertion{fqid, space_id (conf/room id), nonce}`` (reuse
``federation.assertion.build_signed``). ``POST /join/sovereign`` then:

  1. verifies the signature + freshness via ``assertion.verify_signed``
     (same crypto seam the federation path uses), then
  2. rejects replays via the replay-nonce guard (``federation.nonce``), then
  3. mints a SOVEREIGN-grant conference token (``tokens.mint_conf_token`` with
     ``role=ConfRole.SOVEREIGN``) whose **LiveKit identity is the PROVEN fqid**
     — never a caller-supplied identity string.

This mirrors the verification *structure* of ``authd.authorize()`` but with a
LOCAL trust policy. The default policy accepts any assertion that is validly
self-signed by the FQID it claims (the signature itself is the proof of
ownership) — there is no remote allowlist to consult for a local sovereign
joining their own conference. ``room_admin`` is opt-in via the request
(``sovereign_admin``) and only ever honored for the SOVEREIGN role inside the
``conf_grant_for`` factory.
"""

from __future__ import annotations

import os
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from skchat.spaces.federation.assertion import Assertion, verify_signed
from skchat.spaces.federation.nonce import NonceCache
from skchat.spaces.roles import ConfRole

# Replay window — kept in lockstep with the assertion freshness `max_age` so a
# nonce can't outlive the assertion that carried it (same convention as authd).
MAX_AGE = 300

# Process-wide replay cache (single-replica). A multi-replica deployment must
# swap this for a shared store — see federation/nonce.py.
_NONCE = NonceCache()


class JoinDenied(Exception):
    """Raised when a sovereign-join assertion is structurally valid but the
    local policy refuses it (e.g. replay)."""


def _default_mint(identity: str, space_id: str, *, sovereign_admin: bool) -> str:
    from skchat.spaces.tokens import mint_conf_token

    ttl = int(os.getenv("SKCHAT_LIVEKIT_TOKEN_TTL", "21600"))
    # identity == the PROVEN fqid; display name is the bare agent part.
    return mint_conf_token(
        identity,
        identity.split("@")[0],
        ConfRole.SOVEREIGN,
        space_id,
        ttl,
        sovereign_admin=sovereign_admin,
    )


def authorize_sovereign(
    signed: dict,
    *,
    conf_ws_url: str,
    sovereign_admin: bool = False,
    _verify: Callable[..., Assertion] | None = None,
    _mint: Callable[..., str] | None = None,
    _nonce: NonceCache = _NONCE,
) -> dict:
    """Verify a self-signed sovereign assertion and mint a SOVEREIGN conf token.

    Mirrors ``authd.authorize`` structurally (verify -> replay guard -> mint)
    but with the LOCAL trust policy: a valid self-signature over the claimed
    fqid is sufficient proof for a local sovereign joining their own conference.
    The minted token's identity is the PROVEN ``assertion.fqid`` — never a
    caller-supplied string. ``room_admin`` is only requested for SOVEREIGN and
    only when ``sovereign_admin`` is True (enforced by ``conf_grant_for``).
    """
    # Resolve the verifier at call time (None → module-level verify_signed) so the
    # route path is monkeypatchable in tests via the module global.
    verify = _verify or verify_signed
    assertion = verify(signed)
    # Reject replayed assertions (same fqid+nonce within the freshness window)
    # BEFORE minting any token.
    if not _nonce.check_and_add(assertion.fqid, assertion.nonce, MAX_AGE):
        raise JoinDenied("replay detected")
    mint = _mint or _default_mint
    token = mint(assertion.fqid, assertion.space_id, sovereign_admin=sovereign_admin)
    return {
        "conf_ws_url": conf_ws_url,
        "token": token,
        "role": ConfRole.SOVEREIGN.value,
        "identity": assertion.fqid,
        "space_id": assertion.space_id,
        "sovereign_admin": bool(sovereign_admin),
    }


def register_join_routes(app: FastAPI) -> None:
    """Register ``POST /join/sovereign`` on ``app``.

    Thin route: parse the ``{claim, sig, sovereign_admin?}`` body, delegate the
    crypto/replay logic to ``authorize_sovereign`` (unit-tested), and map
    failures to 400 (malformed) / 403 (rejected assertion or replay).
    """

    def _url(request: Request) -> str:
        """Public-aware SFU URL: a sovereign joining from OFF the tailnet (e.g.
        via Tailscale Funnel on cellular) is handed the public wss URL; a genuine
        tailnet caller keeps the tailnet URL. Falls back to the tailnet default
        if the helper is unavailable. See livekit_routes.public_aware_livekit_url."""
        try:
            from skchat.livekit_routes import public_aware_livekit_url

            return public_aware_livekit_url(request)
        except Exception:  # pragma: no cover - defensive fallback
            return os.getenv("SKCHAT_LIVEKIT_URL", "ws://skworld-100:7880")

    @app.post("/join/sovereign")
    async def join_sovereign(request: Request) -> JSONResponse:
        """Local sovereign conference join: verify a capauth-signed FQID
        assertion and mint a SOVEREIGN conference token whose LiveKit identity
        is the proven fqid. Body: ``{claim, sig, sovereign_admin?}``."""
        from skchat.spaces.federation.assertion import (
            AssertionError as FedAssertionError,
        )

        try:
            body = await request.json()
        except Exception as exc:  # malformed / non-JSON body
            raise HTTPException(400, "malformed body: expected JSON") from exc
        if not isinstance(body, dict) or "claim" not in body or "sig" not in body:
            raise HTTPException(400, "body must be {claim, sig}")
        sovereign_admin = bool(body.get("sovereign_admin", False))
        signed = {"claim": body["claim"], "sig": body["sig"]}

        try:
            out = authorize_sovereign(
                signed, conf_ws_url=_url(request), sovereign_admin=sovereign_admin
            )
        except JoinDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except FedAssertionError as exc:
            raise HTTPException(403, f"assertion rejected: {exc}") from exc
        return JSONResponse(out)
