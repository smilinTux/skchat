"""Pydantic models for SKChat — ChatMessage, Thread, and PresenceIndicator.

Every message is a sovereign artifact: it has an identity (sender),
a destination (recipient), PGP encryption, and thread context.
Models here are transport-agnostic — they don't know about SKComms.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


class ContentType(str, Enum):
    """MIME-like content type for chat messages.

    The three core types ship today. The wire/app contract is *extensible*:
    ``ChatMessage.content_type`` is a plain ``str`` (not constrained to this
    enum) so any future type — ``location``, ``file``, ``poll``,
    ``application/skchat.<module>+json`` — deserializes without error. The
    enum stays as the canonical constants for the built-in types and as the
    home of the wire↔app short-form mapping (Golden rule: an unknown type must
    still render via its ``body`` fallback — see :class:`ChatMessage`).
    """

    PLAIN = "text/plain"
    MARKDOWN = "text/markdown"
    SYSTEM = "text/system"

    @classmethod
    def to_wire(cls, value: "ContentType | str") -> str:
        """Map an internal content_type to the short wire/app form.

        ``text/plain``/``text/markdown``/``text/system`` → ``text``/``markdown``
        /``system`` (the exact strings the Flutter contract speaks). Any other
        value (already-short or a future typed form) is returned verbatim, so
        unknown types survive the round-trip untouched.
        """
        raw = value.value if isinstance(value, ContentType) else str(value)
        return {
            cls.PLAIN.value: "text",
            cls.MARKDOWN.value: "markdown",
            cls.SYSTEM.value: "system",
            "text/plain": "text",
            "text/markdown": "markdown",
            "text/system": "system",
        }.get(raw, raw)

    @classmethod
    def from_wire(cls, value: str) -> str:
        """Map a short wire/app form back to the internal content_type string.

        ``text``/``markdown``/``system`` → the canonical ``text/*`` values;
        anything else (a future typed form) passes through unchanged so unknown
        types are never lost.
        """
        return {
            "text": cls.PLAIN.value,
            "markdown": cls.MARKDOWN.value,
            "system": cls.SYSTEM.value,
        }.get(value, value)


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


class EditRecord(BaseModel):
    """One prior version of a message body, captured when the message is edited.

    Attributes:
        body: The message body *before* this edit was applied.
        ts: When this prior version was superseded (i.e. the edit time).
    """

    body: str = Field(description="The message body before this edit")
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Receipts(BaseModel):
    """Delivery / read receipts for a message, keyed by participant fqid.

    Attributes:
        delivered: Senders who have received (delivered) the message.
        read: Senders who have read the message.
    """

    delivered: list[str] = Field(default_factory=list)
    read: list[str] = Field(default_factory=list)


class FileRef(BaseModel):
    """A file/image attached to a chat message (the transfer it rode on).

    Attributes:
        transfer_id: ID of the underlying FileTransferService transfer.
        filename: Original file name.
        size: Size in bytes.
        mime_type: Detected MIME type (e.g. ``image/png``).
        sha256: Whole-file SHA-256 (hex), for integrity display.
        thumbnail_id: Present for images that have a generated thumbnail
            (equals transfer_id; the thumb is served from the transfer dir).
        direction: ``"sent"`` or ``"received"``.
    """

    transfer_id: str
    filename: str
    size: int
    mime_type: str
    sha256: str
    thumbnail_id: Optional[str] = None
    direction: str = "sent"


class ChatMessage(BaseModel):
    """Core message model for SKChat.

    All messages are PGP-encrypted before leaving the crypto layer.
    The envelope wraps the encrypted payload for SKComms transport.

    Attributes:
        id: UUID v4 message identifier.
        sender: CapAuth identity URI of the sender.
        recipient: CapAuth identity URI or group URI.
        content: Plaintext content (encrypted before send).
        content_type: MIME-like content type.
        timestamp: UTC creation time.
        thread_id: Optional thread for threaded conversations.
        reply_to_id: Optional ID of the message being replied to.
            Also accepted as ``reply_to`` during construction.
        reactions: List of reactions on this message.
        metadata: Extensible key-value metadata.
        ttl: Seconds until auto-delete (None = permanent).
        delivery_status: Current delivery lifecycle state.
        encrypted: Whether content is currently PGP-encrypted ciphertext.
        signature: PGP signature over the message content.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    sender: str = Field(description="CapAuth identity URI of the sender")
    recipient: str = Field(description="CapAuth identity URI or group URI")
    content: str = Field(description="Plaintext or PGP-encrypted content")
    content_type: str = Field(
        default=ContentType.MARKDOWN.value,
        description=(
            "MIME-like content type. The built-in values are the ContentType "
            "enum (text/plain, text/markdown, text/system) but ANY string is "
            "accepted so future typed messages (location/file/poll/…) "
            "deserialize without error. Golden rule: an unknown content_type "
            "still renders via `content` (the body fallback)."
        ),
    )
    rich: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Typed payload for non-text content types (forward-compat). None "
            "for plain text/markdown. Dumb clients ignore `rich` and render "
            "`content` as the human-readable fallback."
        ),
    )
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    thread_id: Optional[str] = Field(default=None, description="Thread identifier")
    reply_to_id: Optional[str] = Field(
        default=None,
        description="ID of parent message",
        validation_alias=AliasChoices("reply_to_id", "reply_to"),
    )
    reactions: list[Reaction] = Field(default_factory=list)
    edited_at: Optional[datetime] = Field(
        default=None, description="When this message was last edited (None = never)"
    )
    edit_history: Optional[list[EditRecord]] = Field(
        default=None,
        description="Prior body versions, appended on each edit (None = never edited)",
    )
    receipts: Optional[Receipts] = Field(
        default=None,
        description="Delivery/read receipts by participant (None = none recorded)",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)
    attachments: list[FileRef] = Field(default_factory=list)
    ttl: Optional[int] = Field(default=None, description="Seconds until auto-delete")
    delivery_status: DeliveryStatus = Field(default=DeliveryStatus.PENDING)
    encrypted: bool = Field(default=False)
    signature: Optional[str] = Field(default=None, description="PGP signature armor")

    @property
    def reply_to(self) -> Optional[str]:
        """Alias for reply_to_id for backward compatibility."""
        return self.reply_to_id

    @field_validator("content_type", mode="before")
    @classmethod
    def _normalise_content_type(cls, v: Any) -> str:
        """Coerce content_type to a canonical string, never raising on unknowns.

        - a ``ContentType`` enum → its ``.value`` (``text/plain`` …)
        - a short wire form (``text``/``markdown``/``system``) → canonical ``text/*``
        - any other string (a future typed form) → passed through verbatim
        This is the heart of the Golden rule: unknown types are preserved, not
        rejected, so they always reach a client able to render the ``body``.
        """
        if v is None:
            return ContentType.MARKDOWN.value
        if isinstance(v, ContentType):
            return v.value
        return ContentType.from_wire(str(v))

    @field_validator("sender", "recipient")
    @classmethod
    def identity_must_not_be_empty(cls, v: str) -> str:
        """Ensure sender and recipient are non-empty."""
        if not v.strip():
            raise ValueError("Identity URI cannot be empty")
        return v.strip()

    @model_validator(mode="after")
    def _require_content_or_attachments(self) -> "ChatMessage":
        """A message must carry text content OR at least one attachment."""
        if not (self.content or "").strip() and not self.attachments:
            raise ValueError("Message must have content or at least one attachment")
        return self

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

    # Server-side edit window: edits older than this are refused.
    EDIT_WINDOW_SECONDS: int = 24 * 60 * 60

    def apply_edit(
        self, new_body: str, *, now: Optional[datetime] = None, enforce_window: bool = True
    ) -> None:
        """Replace this message's body, archiving the prior version.

        Appends the *current* body to ``edit_history`` and stamps ``edited_at``.
        Enforces the 24h edit window server-side by default.

        Args:
            new_body: The replacement content.
            now: Override "now" (testing). Defaults to UTC now.
            enforce_window: When True (default) raise if the message is older
                than :data:`EDIT_WINDOW_SECONDS`.

        Raises:
            ValueError: If the edit window has elapsed (when enforced) or the
                new body is empty.
        """
        moment = now or datetime.now(timezone.utc)
        if enforce_window:
            created = self.timestamp
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = (moment - created).total_seconds()
            if age > self.EDIT_WINDOW_SECONDS:
                raise ValueError("edit window elapsed (messages are editable for 24h)")
        if not (new_body or "").strip():
            raise ValueError("edited message body cannot be empty")
        if self.edit_history is None:
            self.edit_history = []
        self.edit_history.append(EditRecord(body=self.content, ts=moment))
        self.content = new_body
        self.edited_at = moment

    def record_receipt(self, kind: str, sender: str) -> bool:
        """Record a delivery/read receipt for *sender*.

        Args:
            kind: ``"delivered"`` or ``"read"``.
            sender: The participant fqid acknowledging.

        Returns:
            bool: True if newly recorded (idempotent — duplicates return False).

        Raises:
            ValueError: If *kind* is not delivered/read.
        """
        if kind not in ("delivered", "read"):
            raise ValueError("receipt kind must be 'delivered' or 'read'")
        who = (sender or "").strip()
        if not who:
            return False
        if self.receipts is None:
            self.receipts = Receipts()
        bucket = self.receipts.delivered if kind == "delivered" else self.receipts.read
        if who in bucket:
            return False
        bucket.append(who)
        return True

    def reactions_map(self) -> dict[str, list[str]]:
        """Aggregate reactions into the wire/app shape ``{emoji: [sender,...]}``."""
        grouped: dict[str, list[str]] = {}
        for r in self.reactions:
            grouped.setdefault(r.emoji, []).append(r.sender)
        return grouped

    def set_reaction(self, emoji: str, sender: str) -> bool:
        """Add a reaction unless this sender already reacted with this emoji.

        Returns:
            bool: True if added, False if it was a duplicate.
        """
        if any(r.emoji == emoji and r.sender == sender for r in self.reactions):
            return False
        self.reactions.append(Reaction(emoji=emoji, sender=sender))
        return True

    def clear_reaction(self, emoji: str, sender: str) -> bool:
        """Remove *sender*'s *emoji* reaction.

        Returns:
            bool: True if a reaction was removed.
        """
        before = len(self.reactions)
        self.reactions = [
            r for r in self.reactions if not (r.emoji == emoji and r.sender == sender)
        ]
        return len(self.reactions) < before

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
