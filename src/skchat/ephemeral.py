"""Ephemeral message enforcer -- TTL expiry and auto-delete for privacy.

Chat messages with a TTL (time-to-live) self-destruct after their
expiry window. This module provides the enforcement engine that
sweeps stored messages and purges expired ones from SKMemory.

The reaper runs periodically (as part of the daemon or standalone)
to enforce TTLs. It also validates incoming messages and rejects
those that arrived already expired.

Privacy guarantee: once a message's TTL expires, its content is
permanently removed from the local memory store. The memory slot
is replaced with a tombstone noting the deletion.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from .models import ChatMessage

logger = logging.getLogger("skchat.ephemeral")


class ExpiryResult(BaseModel):
    """Summary of an expiry sweep.

    Attributes:
        timestamp: When the sweep ran.
        scanned: Total messages checked.
        expired: Number of expired messages deleted.
        tombstoned: Number of tombstone records created.
        errors: Number of deletion failures.
        active_ephemeral: Remaining ephemeral messages still within TTL.
    """

    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    scanned: int = 0
    expired: int = 0
    tombstoned: int = 0
    errors: int = 0
    active_ephemeral: int = 0

    def summary(self) -> str:
        """Human-readable summary.

        Returns:
            str: Formatted summary string.
        """
        return (
            f"Expiry sweep: {self.expired} expired of {self.scanned} scanned, "
            f"{self.active_ephemeral} still active, {self.errors} errors"
        )


class MessageReaper:
    """Enforces TTL expiry on ephemeral chat messages.

    Sweeps the ChatHistory-backed SKMemory store for messages
    with expired TTLs and purges them. Creates tombstone records
    so the UI can show "[message expired]" instead of nothing.

    Args:
        store: An SKMemory MemoryStore instance.
    """

    EPHEMERAL_TAG = "skchat:ephemeral"
    TOMBSTONE_TAG = "skchat:tombstone"

    def __init__(self, store: object) -> None:
        self._store = store

    def sweep(self, create_tombstones: bool = True) -> ExpiryResult:
        """Scan all ephemeral messages and delete expired ones.

        Args:
            create_tombstones: Whether to create tombstone records
                for deleted messages (enables "[expired]" display).

        Returns:
            ExpiryResult: Summary of the sweep.
        """
        result = ExpiryResult()
        now = datetime.now(timezone.utc)

        messages = self._store.list_memories(
            tags=["skchat:message"],
            limit=10000,
        )

        for memory in messages:
            result.scanned += 1

            ttl = memory.metadata.get("ttl")
            if ttl is None:
                continue

            try:
                ttl_seconds = int(ttl)
            except (ValueError, TypeError):
                continue

            created_at = self._parse_timestamp(memory.created_at)
            if created_at is None:
                continue

            expiry_time = created_at + timedelta(seconds=ttl_seconds)

            if now >= expiry_time:
                try:
                    if create_tombstones:
                        self._create_tombstone(memory)
                        result.tombstoned += 1

                    self._store.forget(memory.id)
                    result.expired += 1

                except Exception as exc:
                    logger.warning("Failed to expire memory %s: %s", memory.id[:8], exc)
                    result.errors += 1
            else:
                result.active_ephemeral += 1

        logger.info(result.summary())
        return result

    def is_expired(self, message: ChatMessage) -> bool:
        """Check if a ChatMessage has expired based on its TTL.

        Args:
            message: The message to check.

        Returns:
            bool: True if the message is past its TTL.
        """
        if message.ttl is None:
            return False

        now = datetime.now(timezone.utc)
        expiry = message.timestamp + timedelta(seconds=message.ttl)
        return now >= expiry

    def reject_if_expired(self, message: ChatMessage) -> bool:
        """Check and reject an incoming message if already expired.

        Used during receive to filter out messages that expired
        in transit. Returns True if the message should be rejected.

        Args:
            message: The incoming message.

        Returns:
            bool: True if the message should be rejected (expired).
        """
        if self.is_expired(message):
            logger.info(
                "Rejecting expired message %s (TTL: %ds)",
                message.id[:8], message.ttl,
            )
            return True
        return False

    def time_remaining(self, message: ChatMessage) -> Optional[float]:
        """Get the seconds remaining before a message expires.

        Args:
            message: The message to check.

        Returns:
            Optional[float]: Seconds remaining, or None if permanent.
                Returns 0.0 if already expired.
        """
        if message.ttl is None:
            return None

        now = datetime.now(timezone.utc)
        expiry = message.timestamp + timedelta(seconds=message.ttl)
        remaining = (expiry - now).total_seconds()
        return max(0.0, remaining)

    def tag_ephemeral(self, message: ChatMessage) -> ChatMessage:
        """Add the ephemeral tag to a message for sweep targeting.

        Args:
            message: The message to tag.

        Returns:
            ChatMessage: Copy with the ephemeral tag added.
        """
        if message.ttl is None:
            return message

        metadata = dict(message.metadata)
        metadata["ephemeral"] = True

        return message.model_copy(update={"metadata": metadata})

    def _create_tombstone(self, memory: object) -> None:
        """Create a tombstone record for an expired message.

        The tombstone preserves the sender, recipient, and thread
        but replaces the content with "[message expired]".

        Args:
            memory: The SKMemory Memory object being expired.
        """
        self._store.snapshot(
            title="[expired message]",
            content="[This message has expired and been deleted per sender's TTL policy]",
            tags=[self.TOMBSTONE_TAG, "skchat"],
            source="ephemeral-reaper",
            source_ref=memory.id,
            metadata={
                "original_id": memory.id,
                "sender": memory.metadata.get("sender", ""),
                "recipient": memory.metadata.get("recipient", ""),
                "thread_id": memory.metadata.get("thread_id"),
                "expired_at": datetime.now(timezone.utc).isoformat(),
                "original_ttl": memory.metadata.get("ttl"),
            },
        )

    @staticmethod
    def _parse_timestamp(ts: Any) -> Optional[datetime]:
        """Parse a timestamp string to datetime.

        Args:
            ts: ISO format timestamp string.

        Returns:
            Optional[datetime]: Parsed datetime, or None on failure.
        """
        if isinstance(ts, datetime):
            return ts
        try:
            return datetime.fromisoformat(str(ts))
        except (ValueError, TypeError):
            return None
