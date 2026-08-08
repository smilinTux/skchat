"""Operator endpoints for the "Linked Devices" surface.

Thin by design: parse, authorize, delegate. The list comes from
:mod:`skchat.device_registry` and the revocation work is
:func:`skchat.device_unlink.unlink_device`; nothing security-relevant is decided
here.

Authorization reuses ``guest._require_operator``, which already accepts EITHER
the shared operator token OR an enrolled-operator session Bearer, so the app's
existing auth interceptor works unchanged. Self-lockout protection is stricter
than that, though: single-device DELETE, unlink-others, approve, and deny all
additionally require a resolvable operator SESSION (not just any accepted
credential), because only a session carries the caller's own device_fp --
without it, none of those routes can tell whether a target fingerprint is the
device making the call, so all fail closed with 400 rather than risk acting on
the caller itself.

Phase 3 (approval-to-link): a new enrollment lands pending (see
:mod:`skchat.device_registry`) and cannot mint a session at all, so it cannot
reach any of these routes on its own. ``GET .../pending`` lists rows awaiting
approval (gated like the plain list, no session required); ``POST
.../{fp}/approve`` flips it to approved; ``POST .../{fp}/deny`` is a full
unlink (:func:`skchat.device_unlink.unlink_device`), the row kept for audit.
No separate quarantine for prekey slots exists or is needed: publishing one is
itself an authenticated call a pending device can never make.
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
            # Explicit boolean even for a pre-Phase-3 row with no `approved`
            # key at all, so the app can render the pending banner off one
            # field without also knowing the absence-means-approved rule.
            item["approved"] = DR.is_approved(row)
            rows.append(item)
        return JSONResponse({"devices": rows})

    @router.patch("/devices/{device_fp}")
    async def rename(device_fp: str, request: Request):
        from skchat.guest import _require_operator

        _require_operator(request)
        from skchat import device_registry as DR

        body = await request.json()
        label = body.get("label")
        if not isinstance(label, str) or not label.strip():
            raise HTTPException(400, "label must be a non-empty string")
        text = label.strip()[:64]
        if not DR.set_label(device_fp, text):
            raise HTTPException(404, "device not found")
        row = dict(DR.get_device(device_fp) or {})
        row.pop("user_agent", None)  # internal detail, not UI data
        current = _current_device_fp(request)
        row["is_current"] = bool(current) and row.get("device_fp") == current
        row["approved"] = DR.is_approved(row)
        return JSONResponse(row)

    @router.get("/devices/pending")
    async def pending(request: Request):
        from skchat.guest import _require_operator

        _require_operator(request)
        from skchat import device_registry as DR

        rows = []
        for row in DR.list_pending():
            item = dict(row)
            item.pop("user_agent", None)  # internal detail, not UI data
            item["approved"] = False  # list_pending() only returns unapproved rows
            rows.append(item)
        return JSONResponse({"devices": rows})

    @router.post("/devices/{device_fp}/approve")
    async def approve(device_fp: str, request: Request):
        from skchat.guest import _require_operator

        _require_operator(request)
        from skchat import device_registry as DR

        current = _current_device_fp(request)
        if not current:
            raise HTTPException(
                400,
                "an operator session is required so only an already-approved "
                "device can vouch for a new one",
            )
        # A device whose registry write failed has no row, and approval_for
        # reads that as pending. Approving it must still work, or the only
        # recovery is hand-editing JSON. But a fingerprint that is not enrolled
        # AT ALL is a typo, not a recovery case, so that still 404s.
        if DR.get_device(device_fp) is None and not device_store.is_enrolled(device_fp):
            raise HTTPException(404, "device not found")
        DR.set_approved(device_fp, True)
        row = dict(DR.get_device(device_fp) or {})
        row.pop("user_agent", None)  # internal detail, not UI data
        row["approved"] = DR.is_approved(row)
        return JSONResponse(row)

    @router.post("/devices/{device_fp}/deny")
    async def deny(device_fp: str, request: Request):
        from skchat.guest import _require_operator

        _require_operator(request)
        from skchat import device_unlink as DU

        current = _current_device_fp(request)
        if not current:
            raise HTTPException(
                400,
                "an operator session is required so only an already-approved "
                "device can deny a new one",
            )
        if device_fp == current:
            raise HTTPException(400, "cannot deny the device you are using")
        try:
            return JSONResponse(DU.unlink_device(device_fp, device_store=device_store))
        except KeyError:
            raise HTTPException(404, "device not found")

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
