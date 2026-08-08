"""Operator device registry: the join table for "Linked Devices".

A device carries three unrelated identifiers today (``device_fp`` in the auth
handshake, ``key_id`` on a prekey slot, and a throwaway ``device_id`` inside the
slot) across four stores, with nothing tying them together. This module is that
missing correlation key: one row per ``device_fp``, carrying the metadata the UI
shows and the ``key_ids`` that unlink must remove.

Per-node by design, like the rest of skchat state: a device is managed on the
node it talks to (see the spec's "Out of scope").

Recording is BEST-EFFORT everywhere. A publish from a device that predates the
registry (or that arrived with the auth gate off, so no session was verified)
records nothing and must never break the caller's request.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger("skchat.device_registry")

#: Override the store location (tests point this at tmp_path).
REGISTRY_PATH_ENV = "SKCHAT_DEVICE_REGISTRY"
_DEFAULT_REGISTRY = "~/.skchat/state/operator_device_registry.json"

_lock = threading.Lock()


def registry_path() -> Path:
    raw = os.getenv(REGISTRY_PATH_ENV, "").strip() or _DEFAULT_REGISTRY
    return Path(raw).expanduser()


def _load() -> dict[str, dict]:
    """Read the registry. A missing or corrupt file degrades to empty, never raises."""
    path = registry_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text() or "{}")
    except (ValueError, OSError):
        logger.warning("device registry unreadable, treating as empty: %s", path)
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, dict]) -> None:
    """Atomic write: tmp in the same dir, then os.replace (never a torn file)."""
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def is_approved(row: dict) -> bool:
    """Whether a registry row's device is approved to hold a session.

    A row written before Phase 3 (approval-to-link) shipped has no
    ``approved`` key at all, and those rows belong to devices that were
    already trusted under the old, no-approval model. Absence MUST read as
    approved, never as pending: the operator's 3 live devices carry rows with
    no ``approved`` key, and misreading that as pending locks out every one
    of them at once with no approved device left to approve from.
    """
    return bool(row.get("approved", True))


def record_enroll(
    device_fp: str,
    *,
    label: str,
    label_source: str,
    platform: str,
    user_agent: str,
) -> None:
    """Create (or refresh) the row for a freshly enrolled device.

    A re-enroll of the same fingerprint refreshes the metadata, clears the
    revoked flag (re-linking a device is how you undo an unlink), and
    preserves whatever approval state it already had -- including a missing
    ``approved`` key, which :func:`is_approved` reads as approved. A brand
    NEW fingerprint lands pending (``approved: False``): it must be approved
    by an already-approved device or the CLI before it can mint a session.
    """
    if not device_fp:
        return
    now = time.time()
    with _lock:
        data = _load()
        existing = data.get(device_fp)
        approved = is_approved(existing) if existing is not None else False
        data[device_fp] = {
            "device_fp": device_fp,
            "label": label,
            "label_source": label_source,
            "platform": platform,
            "user_agent": user_agent,
            "enrolled_at": now,
            "last_seen": now,
            "key_ids": list((existing or {}).get("key_ids") or []),
            "revoked": False,
            "approved": approved,
        }
        _save(data)


def record_publish(device_fp: str, key_id: str) -> None:
    """Attach a published prekey slot to its device. No-op for unknown devices."""
    if not (device_fp and key_id):
        return
    with _lock:
        data = _load()
        row = data.get(device_fp)
        if row is None:
            return
        key_ids = list(row.get("key_ids") or [])
        if key_id not in key_ids:
            key_ids.append(key_id)
        row["key_ids"] = key_ids
        row["last_seen"] = time.time()
        data[device_fp] = row
        _save(data)


def touch(device_fp: str) -> None:
    """Bump ``last_seen``. Best-effort; an unknown device is ignored."""
    if not device_fp:
        return
    with _lock:
        data = _load()
        row = data.get(device_fp)
        if row is None:
            return
        row["last_seen"] = time.time()
        data[device_fp] = row
        _save(data)


#: Minimum seconds between two ``last_seen`` writes for the same device. The
#: registry is a whole-file JSON rewrite, so an unthrottled per-request touch
#: would be a write storm on a busy daemon. Minute resolution is far finer than
#: the "3 hours ago" the UI renders.
TOUCH_THROTTLE_SECONDS = 60

_last_touch: dict[str, float] = {}


def touch_throttled(device_fp: str) -> bool:
    """Bump ``last_seen`` at most once per :data:`TOUCH_THROTTLE_SECONDS`.

    Returns True if a write actually happened. Never raises: a failure to record
    liveness must not affect the request that triggered it.
    """
    if not device_fp:
        return False
    now = time.time()
    previous = _last_touch.get(device_fp, 0.0)
    if now - previous < TOUCH_THROTTLE_SECONDS:
        return False
    _last_touch[device_fp] = now
    try:
        touch(device_fp)
        return True
    except Exception:
        logger.debug("last_seen touch failed for %s (best-effort)", device_fp, exc_info=True)
        return False


def set_label(device_fp: str, label: str) -> bool:
    """Operator-set the row's label. True if a row was found and updated.

    Marks ``label_source`` as ``"operator"``: unlike ``"client"`` (device
    asserted it) or ``"derived"`` (server guessed from the User-Agent), an
    operator-set name is one the operator typed themselves, so it is the one
    label source the UI can treat as trusted.
    """
    with _lock:
        data = _load()
        row = data.get(device_fp)
        if row is None:
            return False
        row["label"] = label
        row["label_source"] = "operator"
        data[device_fp] = row
        _save(data)
        return True


def get_device(device_fp: str) -> dict | None:
    with _lock:
        return _load().get(device_fp)


def list_devices(*, include_revoked: bool = False) -> list[dict]:
    """Rows, newest enrollment first. Revoked rows are kept for audit but hidden."""
    with _lock:
        rows = list(_load().values())
    if not include_revoked:
        rows = [r for r in rows if not r.get("revoked")]
    return sorted(rows, key=lambda r: r.get("enrolled_at") or 0, reverse=True)


def set_approved(device_fp: str, approved: bool) -> bool:
    """Approve or un-approve a device. True if a row was found and updated.

    An approve lets the device mint sessions (see
    :func:`skchat.guest.is_device_approved` and
    :func:`skchat.operator_auth.verify_operator_session`); a False is not
    currently exercised by any route (deny does a full unlink instead, which
    both revokes the row via :func:`mark_revoked` and removes it from
    ``DeviceStore``) but is kept honest/reversible for direct callers.
    """
    with _lock:
        data = _load()
        row = data.get(device_fp)
        if row is None:
            return False
        row["approved"] = approved
        data[device_fp] = row
        _save(data)
        return True


def list_pending() -> list[dict]:
    """Rows awaiting approval: not revoked, not yet approved. Newest first."""
    with _lock:
        rows = list(_load().values())
    rows = [r for r in rows if not r.get("revoked") and not is_approved(r)]
    return sorted(rows, key=lambda r: r.get("enrolled_at") or 0, reverse=True)


def mark_revoked(device_fp: str) -> bool:
    """Flag a row revoked. True if a row was found and flagged."""
    with _lock:
        data = _load()
        row = data.get(device_fp)
        if row is None:
            return False
        row["revoked"] = True
        data[device_fp] = row
        _save(data)
        return True


def clear_all() -> int:
    """Wipe every row (the R1 clean cut). Returns how many were removed."""
    with _lock:
        data = _load()
        count = len(data)
        _save({})
        return count
