"""A short, single-use window during which the FIRST device to link is approved.

Approval-to-link means a newly enrolled device lands pending and nothing
auto-approves, because the operator token is a long-lived plaintext secret and a
leaked token alone must never be enough to link a usable device.

That leaves the bootstrap case: right after ``skchat devices reset`` there are no
approved devices, so there is nobody to approve the first one from. Rather than
send the operator back to the terminal for a second command, the reset opens this
window and the next device to enroll is approved.

Why that is not a hole. Opening the window requires running a command ON THE BOX,
which is strictly stronger evidence than holding the token: shell access already
implies total control. The window is bounded (minutes) and strictly single-use,
so it grants one device, once, at a moment the operator deliberately chose.
Contrast with "whenever there are zero approved devices, auto-approve", which is
open for an unbounded period the operator is not watching, and which a leaked
token is enough to walk through.

Deliberately fails CLOSED everywhere: no file, a corrupt file, a clock past
expiry, or an already-consumed window all mean "approve nobody". The cost of
being wrong in the open direction is an unvouched device with full operator
access; the cost of being wrong in the closed direction is one CLI command.

State lives in a small JSON file rather than memory because the CLI opens the
window in one process and the webui consumes it in another.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("skchat.bootstrap_window")

#: Override the state location (tests point this at tmp_path).
WINDOW_PATH_ENV = "SKCHAT_BOOTSTRAP_WINDOW"
_DEFAULT_PATH = "~/.skchat/state/bootstrap_window.json"

#: How long a reset-opened window stays usable. Long enough to pick up a phone
#: and link it, short enough that an unattended box is not standing open.
DEFAULT_TTL_SECONDS = 900


def window_path() -> Path:
    raw = os.getenv(WINDOW_PATH_ENV, "").strip() or _DEFAULT_PATH
    return Path(raw).expanduser()


def _read() -> dict | None:
    """The stored window, or None. Any problem reads as "no window"."""
    path = window_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text() or "{}")
    except (ValueError, OSError):
        logger.warning("bootstrap window unreadable, treating as closed: %s", path)
        return None
    return data if isinstance(data, dict) else None


def open_window(*, ttl: int = DEFAULT_TTL_SECONDS, now: float | None = None) -> float:
    """Open (or replace) the window. Returns the epoch time it expires.

    Replaces rather than stacks: two resets leave one window, not two.
    """
    now = time.time() if now is None else now
    expires_at = now + float(ttl)
    path = window_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps({"expires_at": expires_at, "consumed": False}, indent=2))
    os.replace(tmp, path)
    logger.info("bootstrap approval window opened for %ss", ttl)
    return expires_at


def is_open(*, now: float | None = None) -> bool:
    """Whether a device enrolling right now would be auto-approved."""
    data = _read()
    if data is None or data.get("consumed"):
        return False
    now = time.time() if now is None else now
    try:
        return now < float(data.get("expires_at") or 0)
    except (TypeError, ValueError):
        return False


def consume(*, now: float | None = None) -> bool:
    """Claim the window for one device. True only for the FIRST caller.

    Marks the window consumed before returning True, so a second enrollment
    cannot ride the same window even if it arrives in the same second.
    """
    if not is_open(now=now):
        return False
    data = _read() or {}
    data["consumed"] = True
    path = window_path()
    try:
        tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)
    except OSError:
        # If the window cannot be marked consumed, refuse rather than risk
        # handing it to a second device as well.
        logger.warning("could not consume the bootstrap window; refusing", exc_info=True)
        return False
    logger.info("bootstrap approval window consumed by an enrolling device")
    return True


def close() -> None:
    """Close the window. Idempotent."""
    try:
        window_path().unlink(missing_ok=True)
    except OSError:  # pragma: no cover - best effort
        logger.debug("could not remove the bootstrap window file", exc_info=True)
