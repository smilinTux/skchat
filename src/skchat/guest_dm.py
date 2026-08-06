"""Guest DM contact registry — reusable-link per-guest DM fanout (S2 of the
guest-dm epic, epic ``8685ede6``, depends on S1 ``d964b5a7``).

A ``mode="dm"`` guest group is a 2-seat 1:1 (see ``guest_groups.create_dm_
invite`` / ``DM_SEAT_CAP``). A REUSABLE dm invite (``single_use=False``) may be
shared with a crowd of strangers; without this registry every non-first
stranger would collide on the same 2 seats and get rejected. ``dm_contacts``
maps each guest's stable browser-key fingerprint (``guest_groups.pubkey_
fingerprint``, keyed ``fp``) to the ONE dm group it belongs to, so:

* a brand-new ``fp`` fans out into its own fresh 2-seat group (handled by the
  caller in ``guest_group_routes.guest_join``; this module just records it),
* a returning ``fp`` resolves back to that same group + history.

Storage: SQLite table in the same db file as ``guest_groups``' ``group_
transfers`` table (``~/.skchat/guest_groups.db`` / ``SKCHAT_GUEST_GROUP_DB``),
following the same module-level-helpers-over-a-lock pattern as ``guest_groups.
record_group_transfer`` / ``_connect``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from typing import Optional

from skchat import guest_groups as GG

logger = logging.getLogger("skchat.guest_dm")

_store_lock = threading.Lock()

# New-contact rate limit: how many BRAND NEW fp's a single reusable invite jti
# may mint within the window, before further new arrivals are refused (generic
# 401, no oracle). Existing/returning contacts never count against this.
_RATE_LIMIT_ENV = "SKCHAT_DM_CONTACT_RATE_LIMIT"
_RATE_WINDOW_ENV = "SKCHAT_DM_CONTACT_RATE_WINDOW"
_DEFAULT_RATE_LIMIT = 20
_DEFAULT_RATE_WINDOW = 86400  # 24h


def _rate_limit() -> int:
    try:
        return max(1, int(os.getenv(_RATE_LIMIT_ENV, str(_DEFAULT_RATE_LIMIT))))
    except (TypeError, ValueError):
        return _DEFAULT_RATE_LIMIT


def _rate_window() -> int:
    try:
        return max(60, int(os.getenv(_RATE_WINDOW_ENV, str(_DEFAULT_RATE_WINDOW))))
    except (TypeError, ValueError):
        return _DEFAULT_RATE_WINDOW


def _connect() -> sqlite3.Connection:
    path = GG._db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dm_contacts ("
        "  fp TEXT PRIMARY KEY,"
        "  guest_id TEXT NOT NULL,"
        "  group_id TEXT NOT NULL,"
        "  invite_jti TEXT NOT NULL,"
        "  alias TEXT,"
        "  contact_expires_at REAL,"
        "  status TEXT NOT NULL DEFAULT 'active',"
        "  muted INTEGER NOT NULL DEFAULT 0,"
        "  created_at REAL NOT NULL,"
        "  last_seen_at REAL NOT NULL"
        ")"
    )
    conn.commit()
    return conn


_COLUMNS = (
    "fp, guest_id, group_id, invite_jti, alias, contact_expires_at,"
    " status, muted, created_at, last_seen_at"
)


def _row_to_dict(row) -> dict:
    return {
        "fp": row[0],
        "guest_id": row[1],
        "group_id": row[2],
        "invite_jti": row[3],
        "alias": row[4],
        "contact_expires_at": row[5],
        "status": row[6],
        "muted": bool(row[7]),
        "created_at": row[8],
        "last_seen_at": row[9],
    }


def get_contact(fp: str) -> Optional[dict]:
    """Return the ``dm_contacts`` row for ``fp``, or None if never admitted."""
    fp = (fp or "").strip()
    if not fp:
        return None
    with _store_lock:
        conn = _connect()
        try:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM dm_contacts WHERE fp = ?", (fp,)
            ).fetchone()
        finally:
            conn.close()
    return _row_to_dict(row) if row else None


def upsert_contact(
    *,
    fp: str,
    guest_id: str,
    group_id: str,
    invite_jti: str,
    alias: Optional[str] = None,
    contact_ttl: Optional[int] = None,
    now_fn=None,
) -> dict:
    """Insert or refresh a dm contact — called on EVERY dm admission (join).

    A first-time ``fp`` is inserted with the sidecar ``alias``/``contact_ttl``
    (pre-set on the invite that admitted it) and ``created_at``==``last_seen_
    at``==now. A returning ``fp`` only has ``last_seen_at`` refreshed — its
    group_id/alias/expiry stay pinned to whatever its first admission set.
    """
    fp = (fp or "").strip()
    if not fp:
        raise ValueError("fp is required")
    now = float((now_fn or time.time)())
    with _store_lock:
        conn = _connect()
        try:
            existing = conn.execute("SELECT fp FROM dm_contacts WHERE fp = ?", (fp,)).fetchone()
            if existing:
                conn.execute("UPDATE dm_contacts SET last_seen_at = ? WHERE fp = ?", (now, fp))
            else:
                expires_at = now + contact_ttl if contact_ttl else None
                conn.execute(
                    "INSERT INTO dm_contacts (fp, guest_id, group_id, invite_jti, alias,"
                    " contact_expires_at, status, muted, created_at, last_seen_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, 'active', 0, ?, ?)",
                    (fp, guest_id, group_id, invite_jti, alias, expires_at, now, now),
                )
            conn.commit()
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM dm_contacts WHERE fp = ?", (fp,)
            ).fetchone()
        finally:
            conn.close()
    return _row_to_dict(row)


class ContactRateLimited(Exception):
    """Raised when a reusable invite jti has minted too many fresh contacts.

    Callers MUST map this to a generic 401 (same as ``guest_groups.
    InviteInvalid``) — no oracle distinguishing "rate limited" from "bad
    token".
    """


def check_new_contact_rate(invite_jti: str, *, anchor_group_id: str, now_fn=None) -> None:
    """Raise :class:`ContactRateLimited` if ``invite_jti`` is past its cap.

    Counts FANNED-OUT contacts (rows whose group is NOT the invite's own
    anchor group) created under ``invite_jti`` within the rolling rate window
    (``SKCHAT_DM_CONTACT_RATE_WINDOW``, default 24h) — the guard against a
    shared reusable link being farmed to mint an unbounded number of fresh
    groups. The single guest that fills the anchor's own second seat is
    excluded: that group was already minted at invite-create time and can
    only ever be filled once, so it carries no incremental abuse cost. Only
    call this right before minting a FRESH group for a brand-new contact;
    returning contacts never consume the budget.
    """
    jti = (invite_jti or "").strip()
    if not jti:
        return
    now = float((now_fn or time.time)())
    window_start = now - _rate_window()
    with _store_lock:
        conn = _connect()
        try:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM dm_contacts"
                " WHERE invite_jti = ? AND created_at >= ? AND group_id != ?",
                (jti, window_start, anchor_group_id),
            ).fetchone()
        finally:
            conn.close()
    if count >= _rate_limit():
        raise ContactRateLimited(f"invite {jti!r} exceeded new-contact rate limit")
