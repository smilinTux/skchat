"""SQLite-backed outbox queue for reliable message delivery with retry.

Messages are persisted in ~/.skchat/outbox.db so they survive daemon
restarts. deliver_pending() attempts delivery via an AgentMessenger,
applying exponential backoff on failure and stopping retries after
_MAX_ATTEMPTS failed attempts.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .agent_comm import AgentMessenger

logger = logging.getLogger("skchat.outbox")

_MAX_ATTEMPTS = 5
# Exponential backoff delays in seconds for attempt index 1-5.
_BACKOFF_DELAYS = [5, 15, 45, 120, 600]


def _backoff(attempts: int) -> float:
    """Return seconds to wait before the next retry.

    *attempts* is the post-increment attempt count (1-based), so the first
    failure maps to the shortest delay.
    """
    idx = min(max(0, attempts - 1), len(_BACKOFF_DELAYS) - 1)
    return float(_BACKOFF_DELAYS[idx])


class OutboxQueue:
    """Persistent outbox queue backed by SQLite.

    Stores outgoing text messages and retries delivery until they succeed
    or exhaust _MAX_ATTEMPTS attempts.

    Args:
        db_path: Path to the SQLite database file. Defaults to
            ``~/.skchat/outbox.db``.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            db_path = Path.home() / ".skchat" / "outbox.db"
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS outbox (
                id            TEXT PRIMARY KEY,
                recipient     TEXT NOT NULL,
                content       TEXT NOT NULL,
                thread_id     TEXT,
                attempts      INTEGER NOT NULL DEFAULT 0,
                last_attempt  REAL,
                created_at    REAL NOT NULL,
                delivered     INTEGER NOT NULL DEFAULT 0,
                next_retry_at REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_outbox_pending
            ON outbox(next_retry_at)
            WHERE delivered = 0
            """
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(
        self,
        recipient: str,
        content: str,
        thread_id: Optional[str] = None,
    ) -> str:
        """Add a message to the outbox for delivery.

        Args:
            recipient: CapAuth identity URI of the recipient.
            content: Message text to deliver.
            thread_id: Optional thread ID for conversation grouping.

        Returns:
            str: Unique message ID assigned to this entry.
        """
        msg_id = str(uuid.uuid4())
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO outbox
                (id, recipient, content, thread_id, attempts, created_at,
                 delivered, next_retry_at)
            VALUES
                (?, ?, ?, ?, 0, ?, 0, ?)
            """,
            (msg_id, recipient, content, thread_id, now, now),
        )
        self._conn.commit()
        logger.debug("Enqueued message %s -> %s", msg_id[:8], recipient)
        return msg_id

    def deliver_pending(self, messenger: "AgentMessenger") -> tuple[int, int]:
        """Attempt delivery of all pending messages via AgentMessenger.

        Queries messages where ``attempts < _MAX_ATTEMPTS`` and
        ``delivered = 0`` whose ``next_retry_at`` is in the past.
        On success calls :meth:`mark_delivered`; on failure increments
        the attempt counter and pushes ``next_retry_at`` forward with
        exponential backoff.

        Args:
            messenger: ``AgentMessenger`` instance used for sending.

        Returns:
            tuple[int, int]: ``(delivered_count, failed_count)``
        """
        now = time.time()
        rows = self._conn.execute(
            """
            SELECT id, recipient, content, thread_id, attempts
            FROM   outbox
            WHERE  delivered = 0
              AND  attempts < ?
              AND  next_retry_at <= ?
            ORDER BY created_at
            """,
            (_MAX_ATTEMPTS, now),
        ).fetchall()

        delivered_count = 0
        failed_count = 0

        for row in rows:
            msg_id: str = row["id"]
            recipient: str = row["recipient"]
            content: str = row["content"]
            thread_id: Optional[str] = row["thread_id"]
            attempts: int = row["attempts"]

            success = False
            try:
                result = messenger.send(
                    recipient=recipient,
                    content=content,
                    thread_id=thread_id,
                )
                success = bool(result.get("delivered", False))
            except Exception as exc:
                logger.warning("Delivery failed for %s: %s", msg_id[:8], exc)

            if success:
                self.mark_delivered(msg_id)
                delivered_count += 1
                logger.debug("Delivered message %s -> %s", msg_id[:8], recipient)
            else:
                new_attempts = attempts + 1
                delay = _backoff(new_attempts)
                next_retry = time.time() + delay
                self._conn.execute(
                    """
                    UPDATE outbox
                    SET    attempts = ?, last_attempt = ?, next_retry_at = ?
                    WHERE  id = ?
                    """,
                    (new_attempts, time.time(), next_retry, msg_id),
                )
                logger.debug(
                    "Message %s failed (attempt %d/%d), retry in %.0fs",
                    msg_id[:8],
                    new_attempts,
                    _MAX_ATTEMPTS,
                    delay,
                )
                failed_count += 1

        self._conn.commit()
        return delivered_count, failed_count

    def mark_delivered(self, msg_id: str) -> None:
        """Mark a message as successfully delivered.

        Args:
            msg_id: The message ID to mark delivered.
        """
        self._conn.execute(
            "UPDATE outbox SET delivered = 1, last_attempt = ? WHERE id = ?",
            (time.time(), msg_id),
        )
        self._conn.commit()

    def pending_count(self) -> int:
        """Return the number of messages still awaiting delivery.

        Returns:
            int: Count of messages with ``delivered = 0`` and remaining attempts.
        """
        row = self._conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE delivered = 0 AND attempts < ?",
            (_MAX_ATTEMPTS,),
        ).fetchone()
        return int(row[0])

    def cleanup(self, older_than_days: int = 7) -> int:
        """Remove old delivered messages.

        Args:
            older_than_days: Messages older than this many days that are
                already delivered are removed.

        Returns:
            int: Number of rows removed.
        """
        cutoff = time.time() - older_than_days * 86400
        cursor = self._conn.execute(
            "DELETE FROM outbox WHERE created_at < ? AND delivered = 1",
            (cutoff,),
        )
        self._conn.commit()
        removed = cursor.rowcount
        if removed:
            logger.info("Cleanup removed %d old message(s)", removed)
        return removed

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()
