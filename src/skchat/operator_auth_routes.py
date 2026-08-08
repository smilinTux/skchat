"""FastAPI routes for the operator device-key auth handshake.

Ships dark: these routes exist but nothing is gated on their output until the
enforcement middleware is added in a later task. Enrollment is operator-gated
(loopback/tailnet or SKCHAT_GUEST_OPERATOR_TOKEN) via the existing
guest._require_operator; challenge/session are open by design, they only mint
a session for a device whose key is already enrolled and the request must
carry a valid device signature over the canonical challenge payload.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, FastAPI, HTTPException, Request

from . import operator_auth as oa
from .guest import _require_operator
from .operator_grants import grant_operator_prekey_capability
from .pairing_gate import PairingGate

_pairing = PairingGate(max_accepts_per_window=1)  # operator enroll: 1 device per window


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def register_operator_auth_routes(app: FastAPI, *, device_store: oa.DeviceStore) -> None:
    router = APIRouter(prefix="/api/v1/auth")

    @router.post("/enroll/open")
    async def enroll_open(request: Request):
        _require_operator(request)  # loopback/tailnet or operator token
        window = _pairing.open_window()
        return {"window_nonce": window["nonce"], "exp": window["expires_at"]}

    @router.post("/enroll")
    async def enroll(request: Request):
        body = await request.json()
        pub = body.get("device_pubkey")
        wnonce = body.get("window_nonce")
        sig = body.get("sig")
        if not (pub and wnonce and sig):
            raise HTTPException(400, "device_pubkey, window_nonce, sig required")
        ok, _reason = _pairing.check(wnonce)
        if not ok:
            raise HTTPException(401, "enrollment window closed or invalid")
        if not oa.verify_device_signature(
            device_pubkey_b64=pub,
            payload=_canon({"nonce": wnonce, "device_pubkey": pub}),
            sig_b64=sig,
        ):
            raise HTTPException(401, "device signature invalid")
        _pairing.consume()
        device_fp = device_store.enroll(pub)
        # Grant the enrolled device the skchat.prekey capability so a POST
        # /api/v1/prekey from its session is AUTHORIZED (not just authenticated)
        # when the authz PDP is enforcing. Best-effort: a grant failure is logged
        # inside and never breaks the enrollment response.
        grant_operator_prekey_capability(device_fp, pub)
        return {"device_fp": device_fp}

    @router.get("/challenge")
    async def challenge():
        nonce, exp = oa.issue_challenge()
        return {"nonce": nonce, "exp": exp}

    @router.post("/session")
    async def session(request: Request):
        body = await request.json()
        fp = body.get("device_fp")
        nonce = body.get("nonce")
        sig = body.get("sig")
        if not (fp and nonce and sig):
            raise HTTPException(400, "device_fp, nonce, sig required")
        if not oa.consume_challenge(nonce):
            raise HTTPException(401, "challenge nonce invalid or expired")
        pub = device_store.pubkey_for(fp)
        if not pub:
            raise HTTPException(401, "device not enrolled")
        if not oa.verify_device_signature(
            device_pubkey_b64=pub,
            payload=_canon({"nonce": nonce, "device_fp": fp}),
            sig_b64=sig,
        ):
            raise HTTPException(401, "challenge signature invalid")
        token = oa.mint_operator_session(device_fp=fp)
        sess = oa.verify_operator_session(token)
        resp = {"session_token": token, "expires_at": sess.exp}
        # CR-3.4 AC1 / Phase 1: ALSO mint a parallel capauth audience token for
        # operator:<device_fp>, gated by SKCHAT_OPERATOR_AUDIENCE_ISSUE (default
        # OFF) and NON-FATAL. The HS256 session above stays the primary credential
        # the client uses; this is additive so today's clients (which read only
        # session_token) are unaffected. A mint failure returns None and never
        # breaks the handshake -- the seat cannot be locked out by the audience path.
        from .operator_audience import issue_operator_audience, operator_issuer_policy

        extra = issue_operator_audience(fp)
        if extra:
            resp.update(extra)
        # CR-3.4 PR4 / P6: echo the seat's issuer policy so the client's choice
        # of credential is server-driven and reversible with no app rebuild
        # (hs256 default / prefer-audience / audience-only). Always present.
        resp["issuer_policy"] = operator_issuer_policy()
        return resp

    app.include_router(router)
