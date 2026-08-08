"""Operator endpoints for the "Linked Devices" surface.

Thin by design: parse, authorize, delegate. The list comes from
:mod:`skchat.device_registry` and the revocation work is
:func:`skchat.device_unlink.unlink_device`; nothing security-relevant is decided
here.

Authorization reuses ``guest._require_operator``, which already accepts EITHER
the shared operator token OR an enrolled-operator session Bearer, so the app's
existing auth interceptor works unchanged. Self-lockout protection is stricter
than that, though: both single-device DELETE and unlink-others additionally
require a resolvable operator SESSION (not just any accepted credential),
because only a session carries the caller's own device_fp -- without it,
neither route can tell whether a target fingerprint is the device making the
call, so both fail closed with 400 rather than risk unlinking the caller.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("skchat.device_routes")


def _current_device_fp(request: Request) -> str:
    """The fingerprint of the device making this call, or "" if unknown.

    Read from the caller's own operator session. A caller presenting the shared
    operator token, or an unauthenticated loopback/tailnet caller (the two other
    paths ``guest._require_operator`` accepts), has no device identity, so this
    returns "". Both single-device DELETE and unlink-others treat that as a hard
    400: without a known current device neither route can tell whether a target
    fingerprint IS the caller, so both refuse rather than risk stranding the
    operator by unlinking the device it is using.

    The manual ``verify_operator_session`` fallback below is not test-only
    scaffolding: ``request.state.operator_session`` is only ever populated by
    ``enforce_dataplane_auth`` when ``SKCHAT_DATAPLANE_AUTH=1``. With the gate
    off (the default outside the live daemon), that attribute is never set, so
    this fallback is the ONLY thing that can resolve a caller's device_fp from
    its Bearer token. Removing it would make both DELETE and unlink-others 400
    for every caller whenever the gate is off, not just in tests.
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

        current = _current_device_fp(request)
        if not current:
            raise HTTPException(
                400,
                "an operator session is required so this route can tell whether "
                "you are unlinking the device you are using; if you have no "
                "enrolled device to authenticate with, use `skchat devices reset`",
            )
        if device_fp == current:
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
        # ``unlinked`` keeps its original bare-fp shape (the app client depends on
        # it); ``reports``/``skipped``/``degraded`` add the visibility Task 7's
        # report fields exist for, so a partial unlink is never silent here either.
        #
        # The union of the registry's fingerprints and the DeviceStore's own is
        # required, not just the registry: DR.list_devices() is registry-only,
        # and a device enrolled before this feature existed (or enrolled with
        # SKCHAT_DATAPLANE_AUTH off, so nothing was ever recorded) has a live
        # DeviceStore entry and prekey slot but no registry row. Iterating the
        # registry alone makes that device invisible here, so it survives
        # "unlink all other devices" while the response reports a clean 200.
        unlinked: list[str] = []
        reports: dict[str, dict] = {}
        skipped: list[str] = []
        degraded: list[str] = []
        all_fps = {row["device_fp"] for row in DR.list_devices()} | set(device_store.list_fps())
        for fp in all_fps:
            if fp == current:
                continue
            try:
                report = DU.unlink_device(fp, device_store=device_store)
            except KeyError:
                skipped.append(fp)
                logger.warning("unlink-others: %s vanished before it could be unlinked", fp)
                continue
            unlinked.append(fp)
            reports[fp] = report
            is_degraded = (
                bool(report.get("slots_failed"))
                or bool(report.get("registry_had_no_slots"))
                or (report.get("capauth_records_failed") or 0) > 0
            )
            if is_degraded:
                degraded.append(fp)
                logger.warning("unlink-others: %s unlinked with a degraded report: %s", fp, report)
        return JSONResponse(
            {"unlinked": unlinked, "reports": reports, "skipped": skipped, "degraded": degraded}
        )

    app.include_router(router)
