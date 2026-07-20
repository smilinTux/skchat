"""Temporary debug beacon for the [SPVID] camera-share bug repro.

POST /spvid-log lets any device, including an unauthenticated guest browser
tab where Flutter web release does not reliably surface debugPrint to the
console, push one trace line to a single server-side log file so a
multi-device repro lands in ONE place. Deliberately NOT under /api/v1 so the
operator-auth gate (dataplane_paths.is_gated) never touches it, guest devices
must be able to post without a session.

This is scaffolding for one bug hunt (see commit b0ae75b) and should be
deleted once the bug is diagnosed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

_LOG_PATH = Path("~/.skchat/spvid-debug.log").expanduser()
_MAX_LINE = 2000
_MAX_DEVICE = 32


def register_spvid_debug(app: FastAPI) -> None:
    """Register POST /spvid-log on app. Never raises into the request path."""

    @app.post("/spvid-log")
    async def _spvid_log(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            device = str(body.get("device", "?"))[:_MAX_DEVICE]
            line = str(body.get("line", ""))[:_MAX_LINE]
            t = body.get("t")
            if isinstance(t, (int, float)):
                ts = datetime.fromtimestamp(t / 1000, tz=timezone.utc).isoformat()
            else:
                ts = datetime.now(tz=timezone.utc).isoformat()
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(f"{ts} [{device}] {line}\n")
        except Exception:
            pass
        return JSONResponse({"ok": True})
