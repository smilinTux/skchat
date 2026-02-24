"""Tests for SKChat reactions and annotations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from skchat.reactions import ReactionEvent, ReactionManager, ReactionSummary


@pytest.fixture()
def manager() -> ReactionManager:
    """Fresh ReactionManager."""
    return ReactionManager()


class TestAddRemoveReactions:
    """Tests for adding and removing reactions."""

    def test_add_reaction(self, manager: ReactionManager) -> None:
        """Happy path: add a reaction."""
        assert manager.add_reaction("msg-1", "thumbsup", "capauth:alice@test") is True
        assert manager.total_reactions() == 1

    def test_add_duplicate_rejected(self, manager: ReactionManager) -> None:
        """Same sender + same emoji on same message is deduplicated."""
        manager.add_reaction("msg-1", "thumbsup", "capauth:alice@test")
        assert manager.add_reaction("msg-1", "thumbsup", "capauth:alice@test") is False
        assert manager.total_reactions() == 1

    def test_different_emoji_allowed(self, manager: ReactionManager) -> None:
        """Same sender can add different emoji."""
        manager.add_reaction("msg-1", "thumbsup", "capauth:alice@test")
        manager.add_reaction("msg-1", "heart", "capauth:alice@test")
        assert manager.total_reactions() == 2

    def test_different_sender_allowed(self, manager: ReactionManager) -> None:
        """Different senders can add same emoji."""
        manager.add_reaction("msg-1", "thumbsup", "capauth:alice@test")
        manager.add_reaction("msg-1", "thumbsup", "capauth:bob@test")
        assert manager.total_reactions() == 2

    def test_remove_reaction(self, manager: ReactionManager) -> None:
        """Remove an existing reaction."""
        manager.add_reaction("msg-1", "thumbsup", "capauth:alice@test")
        assert manager.remove_reaction("msg-1", "thumbsup", "capauth:alice@test") is True
        assert manager.total_reactions() == 0

    def test_remove_nonexistent(self, manager: ReactionManager) -> None:
        """Removing a non-existent reaction returns False."""
        assert manager.remove_reaction("msg-1", "nope", "nobody") is False


class TestToggleReaction:
    """Tests for toggle behavior."""

    def test_toggle_adds(self, manager: ReactionManager) -> None:
        """Toggle adds when not present."""
        result = manager.toggle_reaction("msg-1", "fire", "capauth:alice@test")
        assert result is True
        assert manager.has_reaction("msg-1", "fire", "capauth:alice@test")

    def test_toggle_removes(self, manager: ReactionManager) -> None:
        """Toggle removes when already present."""
        manager.add_reaction("msg-1", "fire", "capauth:alice@test")
        result = manager.toggle_reaction("msg-1", "fire", "capauth:alice@test")
        assert result is False
        assert not manager.has_reaction("msg-1", "fire", "capauth:alice@test")


class TestSummarize:
    """Tests for reaction aggregation."""

    def test_summarize_empty(self, manager: ReactionManager) -> None:
        """Empty message has zero reactions."""
        summary = manager.summarize("msg-empty")
        assert summary.total_count == 0
        assert summary.unique_reactors == 0

    def test_summarize_grouped(self, manager: ReactionManager) -> None:
        """Reactions are grouped by emoji with sender lists."""
        manager.add_reaction("msg-1", "thumbsup", "capauth:alice@test")
        manager.add_reaction("msg-1", "thumbsup", "capauth:bob@test")
        manager.add_reaction("msg-1", "heart", "capauth:alice@test")

        summary = manager.summarize("msg-1")
        assert summary.total_count == 3
        assert summary.unique_reactors == 2
        assert len(summary.reactions["thumbsup"]) == 2
        assert len(summary.reactions["heart"]) == 1

    def test_display_format(self, manager: ReactionManager) -> None:
        """Display produces readable format."""
        manager.add_reaction("msg-1", "fire", "capauth:alice@test")
        manager.add_reaction("msg-1", "fire", "capauth:bob@test")
        manager.add_reaction("msg-1", "rocket", "capauth:alice@test")

        display = manager.summarize("msg-1").display()
        assert "fire(2)" in display
        assert "rocket(1)" in display

    def test_display_empty(self, manager: ReactionManager) -> None:
        """Empty summary display is empty string."""
        assert manager.summarize("msg-empty").display() == ""


class TestSyncEvents:
    """Tests for reaction sync via events."""

    def test_add_creates_event(self, manager: ReactionManager) -> None:
        """Adding a reaction creates a sync event."""
        manager.add_reaction("msg-1", "thumbsup", "capauth:alice@test")
        events = manager.pending_events()
        assert len(events) == 1
        assert events[0].action == "add"
        assert events[0].message_id == "msg-1"

    def test_remove_creates_event(self, manager: ReactionManager) -> None:
        """Removing a reaction creates a sync event."""
        manager.add_reaction("msg-1", "thumbsup", "capauth:alice@test")
        manager.remove_reaction("msg-1", "thumbsup", "capauth:alice@test")
        events = manager.pending_events()
        assert len(events) == 2
        assert events[1].action == "remove"

    def test_apply_event(self, manager: ReactionManager) -> None:
        """Applying a remote event updates local state."""
        event = ReactionEvent(
            message_id="msg-remote", emoji="star", sender="capauth:peer@test",
        )
        assert manager.apply_event(event) is True
        assert manager.has_reaction("msg-remote", "star", "capauth:peer@test")

    def test_apply_duplicate_event_rejected(self, manager: ReactionManager) -> None:
        """Same event applied twice is deduplicated."""
        event = ReactionEvent(
            message_id="msg-1", emoji="star", sender="capauth:peer@test",
        )
        manager.apply_event(event)
        assert manager.apply_event(event) is False

    def test_pending_events_since(self, manager: ReactionManager) -> None:
        """pending_events with since filter returns only recent."""
        manager.add_reaction("msg-1", "a", "capauth:alice@test")

        cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)

        events = manager.pending_events(since=cutoff)
        assert len(events) == 0


class TestStats:
    """Tests for reaction statistics."""

    def test_message_count(self, manager: ReactionManager) -> None:
        """message_count tracks messages with reactions."""
        manager.add_reaction("msg-1", "a", "alice")
        manager.add_reaction("msg-2", "b", "bob")
        assert manager.message_count() == 2

    def test_top_reacted(self, manager: ReactionManager) -> None:
        """top_reacted returns most-reacted messages."""
        for i in range(5):
            manager.add_reaction("popular", f"emoji-{i}", f"user-{i}")
        manager.add_reaction("quiet", "a", "alice")

        top = manager.top_reacted(limit=1)
        assert len(top) == 1
        assert top[0][0] == "popular"
        assert top[0][1] == 5
