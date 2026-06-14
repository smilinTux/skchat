"""Tests for SKChat models — ChatMessage, Thread, Reaction."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from skchat.models import (
    ChatMessage,
    ContentType,
    DeliveryStatus,
    FileRef,
    Reaction,
    Thread,
)


class TestChatMessage:
    """Tests for the ChatMessage pydantic model."""

    def test_create_basic_message(self, sample_message: ChatMessage) -> None:
        """Happy path: create a message with required fields."""
        assert sample_message.sender == "capauth:alice@skworld.io"
        assert sample_message.recipient == "capauth:bob@skworld.io"
        assert sample_message.content == "Hello from the sovereign side!"
        assert sample_message.content_type == ContentType.PLAIN
        assert sample_message.delivery_status == DeliveryStatus.PENDING
        assert sample_message.encrypted is False
        assert sample_message.signature is None
        assert sample_message.thread_id is None
        assert sample_message.ttl is None

    def test_message_has_uuid_id(self, sample_message: ChatMessage) -> None:
        """Messages should auto-generate UUID v4 identifiers."""
        assert len(sample_message.id) == 36
        assert sample_message.id.count("-") == 4

    def test_message_has_timestamp(self, sample_message: ChatMessage) -> None:
        """Messages should auto-generate UTC timestamps."""
        assert sample_message.timestamp.tzinfo is not None
        now = datetime.now(timezone.utc)
        assert (now - sample_message.timestamp).total_seconds() < 5

    def test_empty_sender_rejected(self) -> None:
        """Edge case: empty sender should be rejected."""
        with pytest.raises(ValueError, match="Identity URI cannot be empty"):
            ChatMessage(
                sender="",
                recipient="capauth:bob@skworld.io",
                content="test",
            )

    def test_empty_content_rejected(self) -> None:
        """Edge case: empty content should be rejected."""
        with pytest.raises(ValueError, match="content or at least one attachment"):
            ChatMessage(
                sender="capauth:alice@skworld.io",
                recipient="capauth:bob@skworld.io",
                content="   ",
            )

    def test_whitespace_sender_stripped(self) -> None:
        """Whitespace in sender/recipient should be stripped."""
        msg = ChatMessage(
            sender="  capauth:alice@skworld.io  ",
            recipient="  capauth:bob@skworld.io  ",
            content="test",
        )
        assert msg.sender == "capauth:alice@skworld.io"
        assert msg.recipient == "capauth:bob@skworld.io"

    def test_ephemeral_message(self) -> None:
        """Messages with TTL are ephemeral."""
        msg = ChatMessage(
            sender="capauth:alice@skworld.io",
            recipient="capauth:bob@skworld.io",
            content="self-destruct in 60s",
            ttl=60,
        )
        assert msg.is_ephemeral() is True
        assert msg.is_expired() is False

    def test_permanent_message_never_expires(self, sample_message: ChatMessage) -> None:
        """Messages without TTL never expire."""
        assert sample_message.is_ephemeral() is False
        assert sample_message.is_expired() is False

    def test_expired_message(self) -> None:
        """A message past its TTL should report as expired."""
        msg = ChatMessage(
            sender="capauth:alice@skworld.io",
            recipient="capauth:bob@skworld.io",
            content="already expired",
            ttl=0,
            timestamp=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        assert msg.is_expired() is True

    def test_add_reaction(self, sample_message: ChatMessage) -> None:
        """Reactions can be added to messages."""
        sample_message.add_reaction("thumbsup", "capauth:bob@skworld.io")
        assert len(sample_message.reactions) == 1
        assert sample_message.reactions[0].emoji == "thumbsup"
        assert sample_message.reactions[0].sender == "capauth:bob@skworld.io"

    def test_to_summary(self, sample_message: ChatMessage) -> None:
        """Summary should show sender and content preview."""
        summary = sample_message.to_summary()
        assert "capauth:alice@skworld.io" in summary
        assert "Hello from the sovereign side!" in summary

    def test_to_summary_encrypted(self) -> None:
        """Summary of encrypted message should not leak content."""
        msg = ChatMessage(
            sender="capauth:alice@skworld.io",
            recipient="capauth:bob@skworld.io",
            content="PGP CIPHERTEXT HERE",
            encrypted=True,
        )
        summary = msg.to_summary()
        assert "[encrypted]" in summary

    def test_markdown_content_type(self) -> None:
        """Default content type is markdown."""
        msg = ChatMessage(
            sender="capauth:a@test",
            recipient="capauth:b@test",
            content="# Hello",
        )
        assert msg.content_type == ContentType.MARKDOWN


class TestThread:
    """Tests for the Thread pydantic model."""

    def test_create_basic_thread(self, sample_thread: Thread) -> None:
        """Happy path: create a thread with participants."""
        assert sample_thread.title == "Project Discussion"
        assert len(sample_thread.participants) == 2
        assert sample_thread.message_count == 0
        assert sample_thread.parent_thread_id is None

    def test_thread_has_uuid_id(self, sample_thread: Thread) -> None:
        """Threads should auto-generate UUID v4 identifiers."""
        assert len(sample_thread.id) == 36

    def test_add_participant(self, sample_thread: Thread) -> None:
        """New participants can be added to a thread."""
        sample_thread.add_participant("capauth:lumina@skworld.io")
        assert "capauth:lumina@skworld.io" in sample_thread.participants

    def test_add_duplicate_participant(self, sample_thread: Thread) -> None:
        """Adding the same participant twice should be idempotent."""
        count_before = len(sample_thread.participants)
        sample_thread.add_participant("capauth:alice@skworld.io")
        assert len(sample_thread.participants) == count_before

    def test_remove_participant(self, sample_thread: Thread) -> None:
        """Participants can be removed from a thread."""
        result = sample_thread.remove_participant("capauth:bob@skworld.io")
        assert result is True
        assert "capauth:bob@skworld.io" not in sample_thread.participants

    def test_remove_nonexistent_participant(self, sample_thread: Thread) -> None:
        """Removing a non-existent participant returns False."""
        result = sample_thread.remove_participant("capauth:nobody@test")
        assert result is False

    def test_touch_updates_timestamp(self, sample_thread: Thread) -> None:
        """touch() should update timestamp and increment count."""
        old_time = sample_thread.updated_at
        old_count = sample_thread.message_count
        time.sleep(0.01)
        sample_thread.touch()
        assert sample_thread.updated_at >= old_time
        assert sample_thread.message_count == old_count + 1

    def test_empty_participants_allowed(self) -> None:
        """Threads can start with no participants."""
        thread = Thread(title="Empty Thread")
        assert thread.participants == []

    def test_whitespace_participants_stripped(self) -> None:
        """Whitespace-only participants should be stripped out."""
        thread = Thread(
            participants=["  capauth:a@test  ", "  ", "capauth:b@test"],
        )
        assert len(thread.participants) == 2
        assert "capauth:a@test" in thread.participants


class TestReaction:
    """Tests for the Reaction model."""

    def test_create_reaction(self) -> None:
        """Happy path: create a reaction."""
        r = Reaction(emoji="heart", sender="capauth:alice@test")
        assert r.emoji == "heart"
        assert r.sender == "capauth:alice@test"
        assert r.timestamp.tzinfo is not None


def test_fileref_round_trip():
    ref = FileRef(transfer_id="t1", filename="a.png", size=12,
                  mime_type="image/png", sha256="ab"*32, thumbnail_id="th1",
                  direction="sent")
    assert FileRef(**ref.model_dump()) == ref


def test_message_with_attachment_allows_empty_content():
    msg = ChatMessage(
        sender="capauth:a@skworld.io", recipient="capauth:b@skworld.io",
        content="",
        attachments=[FileRef(transfer_id="t1", filename="a.png", size=1,
                             mime_type="image/png", sha256="x", direction="sent")],
    )
    assert msg.attachments[0].filename == "a.png"
    assert msg.content == ""


def test_message_empty_content_and_no_attachments_rejected():
    import pytest
    with pytest.raises(ValueError):
        ChatMessage(sender="capauth:a@skworld.io",
                    recipient="capauth:b@skworld.io", content="   ")


def test_old_message_json_without_attachments_loads():
    data = {"sender": "capauth:a@skworld.io", "recipient": "capauth:b@skworld.io",
            "content": "hi"}
    msg = ChatMessage(**data)
    assert msg.attachments == []


# ---------------------------------------------------------------------------
# QA additions — reply alias, summary truncation, round-trip, edge cases
# ---------------------------------------------------------------------------


class TestReplyAlias:
    """The reply_to / reply_to_id alias is load-bearing for the MCP tool API."""

    def test_reply_to_alias_accepted_on_construction(self) -> None:
        """A message can be built with the `reply_to` alias (MCP tools use it)."""
        msg = ChatMessage(
            sender="capauth:a@test",
            recipient="capauth:b@test",
            content="re",
            reply_to="parent-123",
        )
        assert msg.reply_to_id == "parent-123"
        assert msg.reply_to == "parent-123"

    def test_reply_to_id_canonical_field(self) -> None:
        """The canonical field name also works and the property mirrors it."""
        msg = ChatMessage(
            sender="capauth:a@test",
            recipient="capauth:b@test",
            content="re",
            reply_to_id="parent-456",
        )
        assert msg.reply_to == "parent-456"

    def test_reply_to_none_by_default(self) -> None:
        """No parent → reply_to is None."""
        msg = ChatMessage(sender="capauth:a@test", recipient="capauth:b@test", content="x")
        assert msg.reply_to is None


class TestSummaryTruncation:
    """to_summary() previews at most 80 chars of content."""

    def test_summary_truncates_long_content(self) -> None:
        long = "x" * 200
        msg = ChatMessage(sender="capauth:a@test", recipient="capauth:b@test", content=long)
        summary = msg.to_summary()
        # 80-char preview + "sender: " prefix — the body must be exactly 80 chars.
        body = summary.split(": ", 1)[1]
        assert len(body) == 80

    def test_summary_short_content_intact(self) -> None:
        msg = ChatMessage(sender="capauth:a@test", recipient="capauth:b@test", content="short")
        assert msg.to_summary().endswith("short")


class TestRoundTrip:
    """A ChatMessage must survive JSON serialize → deserialize unchanged."""

    def test_full_message_round_trip(self) -> None:
        msg = ChatMessage(
            sender="capauth:alice@skworld.io",
            recipient="capauth:bob@skworld.io",
            content="round trip",
            content_type=ContentType.MARKDOWN,
            thread_id="t-1",
            reply_to_id="r-1",
            ttl=60,
            metadata={"k": "v"},
            delivery_status=DeliveryStatus.SENT,
        )
        msg.add_reaction("fire", "capauth:bob@skworld.io")
        restored = ChatMessage.model_validate_json(msg.model_dump_json())
        assert restored.id == msg.id
        assert restored.content == "round trip"
        assert restored.thread_id == "t-1"
        assert restored.reply_to_id == "r-1"
        assert restored.ttl == 60
        assert restored.metadata == {"k": "v"}
        assert restored.delivery_status == DeliveryStatus.SENT
        assert len(restored.reactions) == 1
        assert restored.reactions[0].emoji == "fire"


def test_thread_parent_id_round_trip() -> None:
    """A nested thread carries its parent_thread_id through serialization."""
    child = Thread(title="child", parent_thread_id="parent-thread")
    restored = Thread.model_validate_json(child.model_dump_json())
    assert restored.parent_thread_id == "parent-thread"


def test_whitespace_only_recipient_rejected() -> None:
    """A whitespace-only recipient is rejected just like sender."""
    with pytest.raises(ValueError, match="Identity URI cannot be empty"):
        ChatMessage(sender="capauth:a@test", recipient="   ", content="hi")
