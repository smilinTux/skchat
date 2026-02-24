"""Pydantic models for SKChat — ChatMessage, Thread, and PresenceIndicator.

Every message is a sovereign artifact: it has an identity (sender),
a destination (recipient), PGP encryption, and thread context.
Models here are transport-agnostic — they don't know about SKComm.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class ContentType(str, Enum):
    """MIME-like content type for chat messages."""

    PLAIN = "text/plain"
    MARKDOWN = "text/markdown"
    SYSTEM = "text/system"


class DeliveryStatus(str, Enum):
    """Tracks message delivery lifecycle."""

    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


class Reaction(BaseModel):
    """A reaction to a message — emoji or text, by any participant (human or AI).

    Attributes:
        emoji: The reaction emoji or short text.
        sender: CapAuth identity URI of the reactor.
        timestamp: When the reaction was added.
    """

    emoji: str = Field(description="Reaction emoji or short text")
    sender: str = Field(description="CapAuth identity URI of the reactor")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ChatMessage(BaseModel):
    """Core message model for SKChat.

    All messages are PGP-encrypted before leaving the crypto layer.
    The envelope wraps the encrypted payload for SKComm transport.

    Attributes:
        id: UUID v4 message identifier.
        sender: CapAuth identity URI of the sender.
        recipient: CapAuth identity URI or group URI.
        content: Plaintext content (encrypted before send).
        content_type: MIME-like content type.
        timestamp: UTC creation time.
        thread_id: Optional thread for threaded conversations.
        reply_to: Optional ID of the message being replied to.
        reactions: List of reactions on this message.
        metadata: Extensible key-value metadata.
        ttl: Seconds until auto-delete (None = permanent).
        delivery_status: Current delivery lifecycle state.
        encrypted: Whether content is currently PGP-encrypted ciphertext.
        signature: PGP signature over the message content.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    sender: str = Field(description="CapAuth identity URI of the sender")
    recipient: str = Field(description="CapAuth identity URI or group URI")
    content: str = Field(description="Plaintext or PGP-encrypted content")
    content_type: ContentType = Field(default=ContentType.MARKDOWN)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    thread_id: Optional[str] = Field(default=None, description="Thread identifier")
    reply_to: Optional[str] = Field(default=None, description="ID of parent message")
    reactions: list[Reaction] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    ttl: Optional[int] = Field(default=None, description="Seconds until auto-delete")
    delivery_status: DeliveryStatus = Field(default=DeliveryStatus.PENDING)
    encrypted: bool = Field(default=False)
    signature: Optional[str] = Field(default=None, description="PGP signature armor")

    @field_validator("sender", "recipient")
    @classmethod
    def identity_must_not_be_empty(cls, v: str) -> str:
        """Ensure sender and recipient are non-empty."""
        if not v.strip():
            raise ValueError("Identity URI cannot be empty")
        return v.strip()

    @field_validator("content")
    @classmethod
    def content_must_not_be_empty(cls, v: str) -> str:
        """Ensure message has content."""
        if not v.strip():
            raise ValueError("Message content cannot be empty")
        return v

    def is_ephemeral(self) -> bool:
        """Check if this message has a time-to-live.

        Returns:
            bool: True if the message will auto-delete.
        """
        return self.ttl is not None

    def is_expired(self) -> bool:
        """Check if this ephemeral message has expired.

        Returns:
            bool: True if past TTL. Always False for permanent messages.
        """
        if self.ttl is None:
            return False
        age = (datetime.now(timezone.utc) - self.timestamp).total_seconds()
        return age > self.ttl

    def add_reaction(self, emoji: str, sender: str) -> None:
        """Add a reaction to this message.

        Args:
            emoji: Reaction emoji or short text.
            sender: CapAuth identity URI of the reactor.
        """
        self.reactions.append(Reaction(emoji=emoji, sender=sender))

    def to_summary(self) -> str:
        """Create a compact summary for display.

        Returns:
            str: Formatted summary like 'sender: content_preview'.
        """
        preview = self.content[:80] if not self.encrypted else "[encrypted]"
        return f"{self.sender}: {preview}"


class Thread(BaseModel):
    """A conversation thread — a logical grouping of related messages.

    Threads can be nested (reply threads within a channel) or
    standalone (a DM conversation). The thread tracks participants
    and the last activity for efficient UI rendering.

    Attributes:
        id: UUID v4 thread identifier.
        title: Optional human-readable thread title.
        participants: List of CapAuth identity URIs in this thread.
        created_at: When the thread was started.
        updated_at: Last activity timestamp.
        message_count: Total messages in this thread.
        metadata: Extensible key-value metadata.
        parent_thread_id: For nested threads (reply to a thread).
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: Optional[str] = Field(default=None, description="Thread title")
    participants: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    message_count: int = Field(default=0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    parent_thread_id: Optional[str] = Field(default=None)

    @field_validator("participants")
    @classmethod
    def must_have_participants(cls, v: list[str]) -> list[str]:
        """Ensure thread has at least one participant on creation.

        We allow empty lists at construction time (participants added later)
        but strip whitespace from all entries.
        """
        return [p.strip() for p in v if p.strip()]

    def add_participant(self, identity_uri: str) -> None:
        """Add a participant to this thread if not already present.

        Args:
            identity_uri: CapAuth identity URI to add.
        """
        uri = identity_uri.strip()
        if uri and uri not in self.participants:
            self.participants.append(uri)

    def remove_participant(self, identity_uri: str) -> bool:
        """Remove a participant from this thread.

        Args:
            identity_uri: CapAuth identity URI to remove.

        Returns:
            bool: True if the participant was found and removed.
        """
        uri = identity_uri.strip()
        if uri in self.participants:
            self.participants.remove(uri)
            return True
        return False

    def touch(self) -> None:
        """Update the last activity timestamp and increment message count."""
        self.updated_at = datetime.now(timezone.utc)
        self.message_count += 1
