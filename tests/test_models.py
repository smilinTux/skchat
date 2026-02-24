"""Tests for SKChat models — ChatMessage, Thread, Reaction."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from skchat.models import (
    ChatMessage,
    ContentType,
    DeliveryStatus,
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
        with pytest.raises(ValueError, match="Message content cannot be empty"):
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
