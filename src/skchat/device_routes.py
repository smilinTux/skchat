"""Operator endpoints for the "Linked Devices" surface.

Thin by design: parse, authorize, delegate. The list comes from
:mod:`skchat.device_registry` and the revocation work is
:func:`skchat.device_unlink.unlink_device`; nothing security-relevant is decided
here.

Authorization reuses ``guest._require_operator``, which already accepts EITHER
the shared operator token OR an enrolled-operator session Bearer, so the app's
existing auth interceptor works unchanged.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("skchat.device_routes")


def _current_device_fp(request: Request) -> str:
    """The fingerprint of the device making this call, or "" if unknown.

    Read from the caller's own operator session. A caller presenting the shared
    operator token instead of a session has no device identity, which simply
    means no row is marked current and self-unlink cannot be detected. That is
    why unlink-others refuses to run without a known current device.
    """
    session = getattr(request.state, "operator_session", None)
    if session is not None and getattr(session, "device_fp", ""):
        return session.device_fp
    auth = (request.headers.get("authorization") or "").strip()
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        return ""
    try:
        from skchat.operator_auth import verify_operator_session

        return verify_operator_session(token).device_fp
    except Exception:
        return ""


def register_device_routes(app: FastAPI, *, device_store) -> None:
    router = APIRouter(prefix="/api/v1/operator")

    @router.get("/devices")
    async def list_devices(request: Request):
        from skchat.guest import _require_operator

        _require_operator(request)
        from skchat import device_registry as DR

        current = _current_device_fp(request)
        rows = []
        for row in DR.list_devices():
            item = dict(row)
            item.pop("user_agent", None)  # internal detail, not UI data
            item["is_current"] = bool(current) and row["device_fp"] == current
            rows.append(item)
        return JSONResponse({"devices": rows})

    @router.delete("/devices/{device_fp}")
    async def unlink(device_fp: str, request: Request):
        from skchat.guest import _require_operator

        _require_operator(request)
        from skchat import device_unlink as DU

        if device_fp == _current_device_fp(request):
            raise HTTPException(
                400,
                "cannot unlink the device you are using; unlink it from another "
                "device, or use unlink-others",
            )
        try:
            return JSONResponse(DU.unlink_device(device_fp, device_store=device_store))
        except KeyError:
            raise HTTPException(404, "device not found")

    @router.post("/devices/unlink-others")
    async def unlink_others(request: Request):
        from skchat.guest import _require_operator

        _require_operator(request)
        from skchat import device_registry as DR
        from skchat import device_unlink as DU

        current = _current_device_fp(request)
        if not current:
            raise HTTPException(
                400, "unlink-others requires an operator session so this device can be spared"
            )
        unlinked = []
        for row in DR.list_devices():
            fp = row["device_fp"]
            if fp == current:
                continue
            try:
                DU.unlink_device(fp, device_store=device_store)
                unlinked.append(fp)
            except KeyError:
                continue
        return JSONResponse({"unlinked": unlinked})

    app.include_router(router)
