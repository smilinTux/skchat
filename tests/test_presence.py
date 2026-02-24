"""Tests for SKChat presence — PresenceIndicator and PresenceTracker."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from skchat.presence import PresenceIndicator, PresenceState, PresenceTracker


class TestPresenceIndicator:
    """Tests for the PresenceIndicator model."""

    def test_create_online_indicator(self) -> None:
        """Happy path: create an online presence indicator."""
        ind = PresenceIndicator(
            identity_uri="capauth:alice@skworld.io",
            state=PresenceState.ONLINE,
        )
        assert ind.identity_uri == "capauth:alice@skworld.io"
        assert ind.state == PresenceState.ONLINE
        assert not ind.is_stale()
        assert ind.is_active()

    def test_stale_indicator(self) -> None:
        """An old indicator should be detected as stale."""
        ind = PresenceIndicator(
            identity_uri="capauth:alice@skworld.io",
            state=PresenceState.ONLINE,
            timestamp=datetime.now(timezone.utc) - timedelta(seconds=300),
        )
        assert ind.is_stale() is True
        assert ind.is_active() is False

    def test_offline_not_active(self) -> None:
        """Offline indicators should not be active."""
        ind = PresenceIndicator(
            identity_uri="capauth:alice@skworld.io",
            state=PresenceState.OFFLINE,
        )
        assert ind.is_active() is False

    def test_typing_is_active(self) -> None:
        """Typing indicators should be active."""
        ind = PresenceIndicator(
            identity_uri="capauth:alice@skworld.io",
            state=PresenceState.TYPING,
            thread_id="thread-123",
        )
        assert ind.is_active() is True

    def test_custom_status(self) -> None:
        """Indicators support a custom status message."""
        ind = PresenceIndicator(
            identity_uri="capauth:alice@skworld.io",
            state=PresenceState.DND,
            custom_status="In a meeting",
        )
        assert ind.custom_status == "In a meeting"

    def test_explicit_expiry(self) -> None:
        """Indicator with explicit expiry past now should be stale."""
        ind = PresenceIndicator(
            identity_uri="capauth:alice@skworld.io",
            state=PresenceState.ONLINE,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        assert ind.is_stale() is True


class TestPresenceTracker:
    """Tests for the PresenceTracker state manager."""

    def test_update_and_get(self) -> None:
        """Happy path: update and retrieve presence."""
        tracker = PresenceTracker()
        ind = PresenceIndicator(
            identity_uri="capauth:alice@skworld.io",
            state=PresenceState.ONLINE,
        )
        tracker.update(ind)

        retrieved = tracker.get("capauth:alice@skworld.io")
        assert retrieved is not None
        assert retrieved.state == PresenceState.ONLINE

    def test_get_unknown_returns_none(self) -> None:
        """Getting an unknown participant returns None."""
        tracker = PresenceTracker()
        assert tracker.get("capauth:nobody@test") is None

    def test_get_state_unknown_returns_offline(self) -> None:
        """State for unknown participant defaults to OFFLINE."""
        tracker = PresenceTracker()
        assert tracker.get_state("capauth:nobody@test") == PresenceState.OFFLINE

    def test_newer_update_wins(self) -> None:
        """A newer indicator should overwrite an older one."""
        tracker = PresenceTracker()
        old = PresenceIndicator(
            identity_uri="capauth:alice@skworld.io",
            state=PresenceState.ONLINE,
            timestamp=datetime.now(timezone.utc) - timedelta(seconds=10),
        )
        new = PresenceIndicator(
            identity_uri="capauth:alice@skworld.io",
            state=PresenceState.TYPING,
        )
        tracker.update(old)
        tracker.update(new)

        assert tracker.get_state("capauth:alice@skworld.io") == PresenceState.TYPING

    def test_older_update_ignored(self) -> None:
        """An older indicator should not overwrite a newer one."""
        tracker = PresenceTracker()
        new = PresenceIndicator(
            identity_uri="capauth:alice@skworld.io",
            state=PresenceState.TYPING,
        )
        old = PresenceIndicator(
            identity_uri="capauth:alice@skworld.io",
            state=PresenceState.ONLINE,
            timestamp=datetime.now(timezone.utc) - timedelta(seconds=10),
        )
        tracker.update(new)
        tracker.update(old)

        assert tracker.get_state("capauth:alice@skworld.io") == PresenceState.TYPING

    def test_who_is_online(self) -> None:
        """who_is_online returns active participants."""
        tracker = PresenceTracker()
        tracker.update(PresenceIndicator(
            identity_uri="capauth:alice@skworld.io",
            state=PresenceState.ONLINE,
        ))
        tracker.update(PresenceIndicator(
            identity_uri="capauth:bob@skworld.io",
            state=PresenceState.OFFLINE,
        ))
        tracker.update(PresenceIndicator(
            identity_uri="capauth:lumina@skworld.io",
            state=PresenceState.TYPING,
        ))

        online = tracker.who_is_online()
        assert "capauth:alice@skworld.io" in online
        assert "capauth:lumina@skworld.io" in online
        assert "capauth:bob@skworld.io" not in online

    def test_who_is_typing(self) -> None:
        """who_is_typing filters by thread when specified."""
        tracker = PresenceTracker()
        tracker.update(PresenceIndicator(
            identity_uri="capauth:alice@skworld.io",
            state=PresenceState.TYPING,
            thread_id="thread-1",
        ))
        tracker.update(PresenceIndicator(
            identity_uri="capauth:bob@skworld.io",
            state=PresenceState.TYPING,
            thread_id="thread-2",
        ))

        all_typing = tracker.who_is_typing()
        assert len(all_typing) == 2

        thread_typing = tracker.who_is_typing(thread_id="thread-1")
        assert len(thread_typing) == 1
        assert "capauth:alice@skworld.io" in thread_typing

    def test_remove(self) -> None:
        """Removing a tracked participant returns True."""
        tracker = PresenceTracker()
        tracker.update(PresenceIndicator(
            identity_uri="capauth:alice@skworld.io",
            state=PresenceState.ONLINE,
        ))
        assert tracker.remove("capauth:alice@skworld.io") is True
        assert tracker.get("capauth:alice@skworld.io") is None

    def test_remove_nonexistent(self) -> None:
        """Removing an untracked participant returns False."""
        tracker = PresenceTracker()
        assert tracker.remove("capauth:nobody@test") is False

    def test_prune_stale(self) -> None:
        """prune_stale removes old indicators."""
        tracker = PresenceTracker()
        tracker.update(PresenceIndicator(
            identity_uri="capauth:stale@skworld.io",
            state=PresenceState.ONLINE,
            timestamp=datetime.now(timezone.utc) - timedelta(seconds=300),
        ))
        tracker.update(PresenceIndicator(
            identity_uri="capauth:fresh@skworld.io",
            state=PresenceState.ONLINE,
        ))

        removed = tracker.prune_stale()
        assert removed == 1
        assert tracker.tracked_count == 1

    def test_tracked_count(self) -> None:
        """tracked_count reflects total tracked participants."""
        tracker = PresenceTracker()
        assert tracker.tracked_count == 0

        for i in range(5):
            tracker.update(PresenceIndicator(
                identity_uri=f"capauth:user{i}@test",
                state=PresenceState.ONLINE,
            ))
        assert tracker.tracked_count == 5
