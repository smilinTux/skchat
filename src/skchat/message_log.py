"""Single-writer, append-only message log (SEAM 2 + SEAM 4).

The authoritative-store foundation: a per-conversation, monotonic ``seq`` and an
immutable server ``message_id``, assigned ATOMICALLY inside one transaction so no
two messages ever share a seq (the single-writer invariant). Writes are
idempotent — a repeat ``client_dedup_key`` (or a supplied ``message_id``) returns
the existing row flagged ``deduped=True`` and never burns a second seq.

This is a PARALLEL authoritative projection: it does NOT touch ``history.py``,
the JSONL store, or the group/DM stores. Those stay the source of truth until a
later phase promotes this log. Storage is stdlib ``sqlite3`` in WAL mode.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skchat.message_log")


def _skchat_home() -> Path:
    """Resolve the skchat home dir, honouring ``SKCHAT_HOME`` (mirrors history)."""
    return Path(os.environ.get("SKCHAT_HOME") or "~/.skchat").expanduser()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MessageLog:
    """Single-writer, append-only per-conversation message log.

    Args:
        db_path: Path to the SQLite database file. Defaults to
            ``$SKCHAT_HOME/message_log.db`` (``~/.skchat/message_log.db``).
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            db_path = _skchat_home() / "message_log.db"
        self._db_path = Path(db_path)
        # Create the parent dir BEFORE connect (LaneStore-style) so a fresh
        # ``$SKCHAT_HOME`` / nested path doesn't fail on first write.
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None -> autocommit; we drive BEGIN IMMEDIATE ourselves
        # so seq assignment is one explicit, serialized transaction.
        self._conn = sqlite3.connect(
            str(self._db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        # In-process single-writer lock: serializes appends across threads that
        # share this connection. BEGIN IMMEDIATE additionally serializes writers
        # across processes (fail-fast on a busy DB).
        self._lock = threading.Lock()
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def _init_schema(self) -> None:
        conn = self._conn
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS message_log (
                conversation_id  TEXT    NOT NULL,
                seq              INTEGER NOT NULL,
                message_id       TEXT    NOT NULL,
                client_dedup_key TEXT,
                sender           TEXT    NOT NULL,
                recipient        TEXT    NOT NULL,
                content          TEXT    NOT NULL,
                ts               TEXT    NOT NULL,
                kind             TEXT    NOT NULL DEFAULT 'text',
                PRIMARY KEY (conversation_id, seq)
            )
            """
        )
        # Immutable server id — globally unique.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_msglog_message_id "
            "ON message_log(message_id)"
        )
        # Idempotency key — unique per conversation (NULLs are exempt in SQLite,
        # so rows without a client_dedup_key never collide).
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_msglog_dedup "
            "ON message_log(conversation_id, client_dedup_key)"
        )

    # ------------------------------------------------------------------
    # Row shaping
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        return {
            "conversation_id": row["conversation_id"],
            "seq": row["seq"],
            "message_id": row["message_id"],
            "client_dedup_key": row["client_dedup_key"],
            "sender": row["sender"],
            "recipient": row["recipient"],
            "content": row["content"],
            "ts": row["ts"],
            "kind": row["kind"],
        }

    def _find_existing(
        self, conversation_id: str, message_id: Optional[str], client_dedup_key: Optional[str]
    ) -> Optional[sqlite3.Row]:
        """Return an existing row matching the dedup key or message_id, else None."""
        if client_dedup_key is not None:
            row = self._conn.execute(
                "SELECT * FROM message_log WHERE conversation_id=? AND client_dedup_key=?",
                (conversation_id, client_dedup_key),
            ).fetchone()
            if row is not None:
                return row
        if message_id is not None:
            row = self._conn.execute(
                "SELECT * FROM message_log WHERE message_id=?", (message_id,)
            ).fetchone()
            if row is not None:
                return row
        return None

    # ------------------------------------------------------------------
    # Append (the single writer)
    # ------------------------------------------------------------------
    def append(
        self,
        conversation_id: str,
        *,
        message_id: Optional[str] = None,
        client_dedup_key: Optional[str] = None,
        sender: str,
        recipient: str,
        content: str,
        kind: str = "text",
    ) -> dict:
        """Append a message; assign a monotonic per-conversation ``seq`` + an
        immutable ``message_id`` (generated if ``None``) in ONE transaction.

        Idempotent: a repeat ``client_dedup_key`` (or a repeat supplied
        ``message_id``) returns the existing row with ``deduped=True`` and does
        NOT assign a second seq.

        Returns a dict:
            ``{conversation_id, seq, message_id, client_dedup_key, sender,
               recipient, content, ts, kind, deduped}``.
        """
        conn = self._conn
        with self._lock:
            # BEGIN IMMEDIATE takes the write lock up front so the MAX(seq)+1
            # read and the INSERT are one indivisible step — no two writers can
            # observe the same MAX and hand out a duplicate seq.
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._find_existing(conversation_id, message_id, client_dedup_key)
                if existing is not None:
                    conn.execute("COMMIT")
                    return {**self._row_to_dict(existing), "deduped": True}

                mid = message_id or str(uuid.uuid4())
                ts = _now_iso()
                conn.execute(
                    """
                    INSERT INTO message_log
                        (conversation_id, seq, message_id, client_dedup_key,
                         sender, recipient, content, ts, kind)
                    VALUES (
                        ?,
                        (SELECT COALESCE(MAX(seq), 0) + 1 FROM message_log
                         WHERE conversation_id = ?),
                        ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        conversation_id, conversation_id, mid, client_dedup_key,
                        sender, recipient, content, ts, kind,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM message_log WHERE message_id=?", (mid,)
                ).fetchone()
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return {**self._row_to_dict(row), "deduped": False}

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def read(self, conversation_id: str, since_seq: int = 0, limit: int = 500) -> list[dict]:
        """Return rows for *conversation_id* with ``seq > since_seq``, ascending."""
        rows = self._conn.execute(
            """
            SELECT * FROM message_log
            WHERE conversation_id = ? AND seq > ?
            ORDER BY seq ASC
            LIMIT ?
            """,
            (conversation_id, since_seq, limit),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def latest_seq(self, conversation_id: str) -> int:
        """Return the highest assigned ``seq`` for *conversation_id* (0 if none)."""
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS s FROM message_log WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        return int(row["s"])

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # pragma: no cover
            logger.debug("message_log close failed", exc_info=True)
