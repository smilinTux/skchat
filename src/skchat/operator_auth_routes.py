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
        return {"device_fp": device_store.enroll(pub)}

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
        return {"session_token": token, "expires_at": sess.exp}

    app.include_router(router)
