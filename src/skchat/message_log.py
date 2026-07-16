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

import hashlib
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


def conversation_id_for(message) -> str:
    """Canonical conversation id for a ``ChatMessage`` (direction-independent).

    Group message -> ``group:<gid>`` (the canonical group copy already carries
    ``recipient="group:<gid>"``; a member copy that names its group thread maps
    there too). 1:1 DM -> ``dm:<a>|<b>`` with the two participant URIs sorted, so
    both directions of a DM collapse to ONE conversation. This is the single
    definition every writer/reader must share.
    """
    recipient = (getattr(message, "recipient", "") or "").strip()
    if recipient.startswith("group:"):
        return recipient
    # A group message ALWAYS carries metadata.group_id (on the canonical copy AND
    # every per-member copy), so this maps any copy to the one group conversation
    # even when the copy is addressed to a member. This is what makes record_event
    # safe to call on any copy: they all resolve here and dedup to one row.
    meta = getattr(message, "metadata", None)
    gid = (meta.get("group_id") if isinstance(meta, dict) else None) or ""
    if gid:
        return gid if str(gid).startswith("group:") else f"group:{gid}"
    thread = (getattr(message, "thread_id", "") or "").strip()
    if thread.startswith("group:"):
        return thread
    sender = (getattr(message, "sender", "") or "").strip()
    a, b = sorted([sender, recipient])
    return f"dm:{a}|{b}"


def dedup_key_for(message) -> str:
    """Idempotency key that collapses the fan-out copies of ONE logical message.

    The ``1+N`` fan-out copies share sender+conversation+content+second but carry
    different ``id``s, so a key over those fields (not the id) lets a re-record or
    a backfill of any copy resolve to the same log row. Sub-second identical
    resends are treated as the same event (an accepted idempotency edge).
    """
    sender = (getattr(message, "sender", "") or "").strip()
    conv = conversation_id_for(message)
    content = getattr(message, "content", "") or ""
    ts = getattr(message, "timestamp", None)
    ts_s = ""
    if ts is not None:
        try:
            ts_s = ts.replace(microsecond=0).isoformat()
        except Exception:
            ts_s = str(ts)
    raw = f"{sender}|{conv}|{content}|{ts_s}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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
                payload          TEXT,
                PRIMARY KEY (conversation_id, seq)
            )
            """
        )
        # Additive migration for logs created before the payload column existed.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(message_log)")}
        if "payload" not in cols:
            conn.execute("ALTER TABLE message_log ADD COLUMN payload TEXT")
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
            "payload": row["payload"] if "payload" in row.keys() else None,
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
        payload: Optional[str] = None,
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
                         sender, recipient, content, ts, kind, payload)
                    VALUES (
                        ?,
                        (SELECT COALESCE(MAX(seq), 0) + 1 FROM message_log
                         WHERE conversation_id = ?),
                        ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        conversation_id, conversation_id, mid, client_dedup_key,
                        sender, recipient, content, ts, kind, payload,
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

    def record(self, message) -> dict:
        """Append a ``ChatMessage`` to the log, deriving the canonical
        ``conversation_id`` + idempotency key. Idempotent by the message's own
        ``id`` and by ``dedup_key_for`` (so re-recording any fan-out copy or a
        backfill re-run resolves to the same row). Returns the append dict
        (``deduped=True`` when it already existed).
        """
        mt = getattr(message, "message_type", None)
        kind = str(getattr(mt, "value", mt) or "text")
        # Store the FULL message JSON so the log is a complete source of truth
        # (reactions, attachments, reply_to, metadata all survive a read).
        try:
            payload = message.model_dump_json()
        except Exception:
            payload = None
        return self.append(
            conversation_id_for(message),
            message_id=(getattr(message, "id", None) or None),
            client_dedup_key=dedup_key_for(message),
            sender=(getattr(message, "sender", "") or ""),
            recipient=(getattr(message, "recipient", "") or ""),
            content=(getattr(message, "content", "") or ""),
            kind=kind,
            payload=payload,
        )

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

    def update_payload(self, message_id: str, payload: str) -> bool:
        """Refresh the stored ``payload`` of an existing logical message.

        A mutation (reaction / edit / receipt) targets the immutable
        ``message_id`` and updates the ONE logical message's materialized state,
        so every surface reading the log sees it (this is what fixes the old
        "a reaction only reaches one fan-out copy" bug). The seq/message_id are
        never touched. Returns True if a row was updated.
        """
        if not message_id:
            return False
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "UPDATE message_log SET payload=? WHERE message_id=?",
                    (payload, message_id),
                )
                conn.execute("COMMIT")
                return cur.rowcount > 0
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def recent(self, limit: int = 50) -> list[dict]:
        """Most recent messages across ALL conversations, newest first (an inbox
        view). Reads the one authoritative log so the inbox matches every other
        surface and reflects mutations."""
        rows = self._conn.execute(
            "SELECT * FROM message_log ORDER BY ts DESC, seq DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def conversations(self, limit: int = 200) -> list[dict]:
        """The latest event per conversation, newest first (a thread list)."""
        rows = self._conn.execute(
            """
            SELECT * FROM message_log m1
            WHERE seq = (
                SELECT MAX(seq) FROM message_log m2
                WHERE m2.conversation_id = m1.conversation_id
            )
            ORDER BY ts DESC LIMIT ?
            """,
            (limit,),
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


def log_row_to_message(row: dict):
    """Reconstruct a full ``ChatMessage`` from a log row.

    Prefers the stored ``payload`` (complete message incl. reactions/attachments/
    reply_to/metadata); falls back to the flat columns for pre-payload rows so a
    partially-backfilled log still reads. Readers use this to serve log-sourced
    history without losing rich message data.
    """
    from skchat.models import ChatMessage

    payload = row.get("payload")
    if payload:
        try:
            return ChatMessage.model_validate_json(payload)
        except Exception:  # noqa: BLE001 — fall back to flat columns
            logger.debug("log payload parse failed for %s", row.get("message_id"))
    return ChatMessage(
        id=row.get("message_id"),
        sender=row.get("sender", "") or "",
        recipient=row.get("recipient", "") or "",
        content=row.get("content", "") or "",
    )
