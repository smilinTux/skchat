"""Operator observability for calls (coord e8651a65).

When agents call each other, alert the operator (Chef) over sk-alert with *who*,
the *topic*, and a **one-press join link** to the LiveKit room — so he knows what
we're working on and can jump in. Built on the deterministic room (A) + the chef
LiveKit identity, so the join token drops him straight into the same room.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

logger = logging.getLogger("skchat.call_observability")

_JOIN_TTL = 21600  # 6h


def _webui_base() -> str:
    return os.getenv("SKCHAT_WEBUI_PUBLIC_URL", "https://noroc2027.tail204f0c.ts.net").rstrip("/")


def _mint_chef_token(room: str) -> str:
    from .livekit_routes import _mint_token

    return _mint_token("chef", "Chef", room, _JOIN_TTL)


def _sk_alert(message: str) -> None:
    alert = shutil.which("sk-alert") or os.path.expanduser("~/.skenv/bin/sk-alert")
    try:
        subprocess.run([alert, "-l", "info", message], timeout=30, check=False)
    except Exception as exc:  # noqa: BLE001 — alerting must never break a call
        logger.warning("operator alert failed: %s", exc)


def operator_join_url(room: str, *, token: str | None = None) -> str:
    """One-press URL that drops the operator into ``room`` as ``chef``."""
    tok = token if token is not None else _mint_chef_token(room)
    base = _webui_base()
    return f"{base}/livekit?room={room}&identity=chef&token={tok}"


def alert_operator(*, from_fqid: str, to_fqid: str, room: str, topic: str = "") -> None:
    """Notify the operator that a call started, with topic + a one-press join link.

    Never raises — observability must not block the call.
    """
    try:
        a = from_fqid.split("@", 1)[0]
        b = to_fqid.split("@", 1)[0]
        url = operator_join_url(room)
        msg = f"📞 {a} & {b} are in a call"
        if topic:
            msg += f" — topic: {topic}"
        msg += f"\nJoin: {url}"
        _sk_alert(msg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("alert_operator failed: %s", exc)
