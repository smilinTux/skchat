"""Tests for SKChat ephemeral message enforcer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from skchat.ephemeral import ExpiryResult, MessageReaper
from skchat.models import ChatMessage


class FakeMemory:
    """Minimal Memory-like object."""

    def __init__(self, id: str, tags: list, metadata: dict, created_at: str) -> None:
        self.id = id
        self.tags = tags
        self.metadata = metadata
        self.created_at = created_at
        self.title = "test"
        self.content = "test content"


class FakeStore:
    """In-memory fake of SKMemory MemoryStore."""

    def __init__(self) -> None:
        self._memories: list[FakeMemory] = []
        self._snapshots: list[dict] = []
        self._forgotten: list[str] = []

    def add(self, memory: FakeMemory) -> None:
        """Add a test memory."""
        self._memories.append(memory)

    def list_memories(self, tags: Optional[list] = None, limit: int = 50, **kw: Any) -> list:
        """List with tag filtering."""
        result = []
        for m in self._memories:
            if tags and not all(t in m.tags for t in tags):
                continue
            result.append(m)
        return result[:limit]

    def forget(self, memory_id: str) -> bool:
        """Delete a memory."""
        self._forgotten.append(memory_id)
        self._memories = [m for m in self._memories if m.id != memory_id]
        return True

    def snapshot(self, **kwargs: Any) -> FakeMemory:
        """Record a snapshot (tombstone)."""
        self._snapshots.append(kwargs)
        return FakeMemory(id="tomb-1", tags=[], metadata={}, created_at="")


@pytest.fixture()
def store() -> FakeStore:
    """Fresh fake store with test messages."""
    s = FakeStore()

    expired_time = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    s.add(FakeMemory(
        id="msg-expired",
        tags=["skchat", "skchat:message"],
        metadata={"ttl": 60, "sender": "alice", "recipient": "bob"},
        created_at=expired_time,
    ))

    fresh_time = datetime.now(timezone.utc).isoformat()
    s.add(FakeMemory(
        id="msg-fresh",
        tags=["skchat", "skchat:message"],
        metadata={"ttl": 3600, "sender": "alice", "recipient": "bob"},
        created_at=fresh_time,
    ))

    s.add(FakeMemory(
        id="msg-permanent",
        tags=["skchat", "skchat:message"],
        metadata={"sender": "alice", "recipient": "bob"},
        created_at=fresh_time,
    ))

    return s


@pytest.fixture()
def reaper(store: FakeStore) -> MessageReaper:
    """MessageReaper wired to the fake store."""
    return MessageReaper(store=store)


class TestSweep:
    """Tests for the expiry sweep."""

    def test_sweep_deletes_expired(self, reaper: MessageReaper, store: FakeStore) -> None:
        """Sweep deletes messages past their TTL."""
        result = reaper.sweep()
        assert result.expired == 1
        assert "msg-expired" in store._forgotten

    def test_sweep_keeps_fresh(self, reaper: MessageReaper, store: FakeStore) -> None:
        """Sweep keeps messages still within TTL."""
        reaper.sweep()
        remaining_ids = [m.id for m in store._memories]
        assert "msg-fresh" in remaining_ids

    def test_sweep_ignores_permanent(self, reaper: MessageReaper) -> None:
        """Sweep ignores messages without TTL."""
        result = reaper.sweep()
        assert result.scanned == 3

    def test_sweep_creates_tombstones(self, reaper: MessageReaper, store: FakeStore) -> None:
        """Sweep creates tombstone records for expired messages."""
        result = reaper.sweep(create_tombstones=True)
        assert result.tombstoned == 1
        assert len(store._snapshots) == 1
        tombstone = store._snapshots[0]
        assert tombstone["title"] == "[expired message]"
        assert tombstone["metadata"]["original_id"] == "msg-expired"

    def test_sweep_no_tombstones(self, reaper: MessageReaper, store: FakeStore) -> None:
        """Sweep without tombstones just deletes."""
        result = reaper.sweep(create_tombstones=False)
        assert result.expired == 1
        assert result.tombstoned == 0
        assert len(store._snapshots) == 0

    def test_sweep_tracks_active_ephemeral(self, reaper: MessageReaper) -> None:
        """Sweep counts remaining active ephemeral messages."""
        result = reaper.sweep()
        assert result.active_ephemeral == 1

    def test_sweep_empty_store(self) -> None:
        """Sweep on empty store produces clean result."""
        empty = FakeStore()
        reaper = MessageReaper(store=empty)
        result = reaper.sweep()
        assert result.scanned == 0
        assert result.expired == 0


class TestIsExpired:
    """Tests for individual message expiry check."""

    def test_expired_message(self, reaper: MessageReaper) -> None:
        """Message past TTL is expired."""
        msg = ChatMessage(
            sender="a@test", recipient="b@test", content="old",
            ttl=10,
            timestamp=datetime.now(timezone.utc) - timedelta(seconds=20),
        )
        assert reaper.is_expired(msg) is True

    def test_fresh_message(self, reaper: MessageReaper) -> None:
        """Message within TTL is not expired."""
        msg = ChatMessage(
            sender="a@test", recipient="b@test", content="new",
            ttl=3600,
        )
        assert reaper.is_expired(msg) is False

    def test_permanent_message(self, reaper: MessageReaper) -> None:
        """Message without TTL never expires."""
        msg = ChatMessage(
            sender="a@test", recipient="b@test", content="forever",
        )
        assert reaper.is_expired(msg) is False


class TestRejectIfExpired:
    """Tests for incoming message rejection."""

    def test_reject_expired_incoming(self, reaper: MessageReaper) -> None:
        """Expired incoming message is rejected."""
        msg = ChatMessage(
            sender="a@test", recipient="b@test", content="stale",
            ttl=5,
            timestamp=datetime.now(timezone.utc) - timedelta(seconds=10),
        )
        assert reaper.reject_if_expired(msg) is True

    def test_accept_fresh_incoming(self, reaper: MessageReaper) -> None:
        """Fresh incoming message is accepted."""
        msg = ChatMessage(
            sender="a@test", recipient="b@test", content="fresh",
            ttl=3600,
        )
        assert reaper.reject_if_expired(msg) is False


class TestTimeRemaining:
    """Tests for time remaining calculation."""

    def test_time_remaining_positive(self, reaper: MessageReaper) -> None:
        """Fresh message has positive time remaining."""
        msg = ChatMessage(
            sender="a@test", recipient="b@test", content="test",
            ttl=3600,
        )
        remaining = reaper.time_remaining(msg)
        assert remaining is not None
        assert remaining > 3500

    def test_time_remaining_expired(self, reaper: MessageReaper) -> None:
        """Expired message has 0.0 remaining."""
        msg = ChatMessage(
            sender="a@test", recipient="b@test", content="test",
            ttl=5,
            timestamp=datetime.now(timezone.utc) - timedelta(seconds=10),
        )
        assert reaper.time_remaining(msg) == 0.0

    def test_time_remaining_permanent(self, reaper: MessageReaper) -> None:
        """Permanent message returns None."""
        msg = ChatMessage(
            sender="a@test", recipient="b@test", content="test",
        )
        assert reaper.time_remaining(msg) is None


class TestTagEphemeral:
    """Tests for ephemeral tagging."""

    def test_tag_adds_metadata(self, reaper: MessageReaper) -> None:
        """Ephemeral message gets tagged in metadata."""
        msg = ChatMessage(
            sender="a@test", recipient="b@test", content="secret",
            ttl=60,
        )
        tagged = reaper.tag_ephemeral(msg)
        assert tagged.metadata.get("ephemeral") is True

    def test_tag_noop_for_permanent(self, reaper: MessageReaper) -> None:
        """Permanent message is returned unchanged."""
        msg = ChatMessage(
            sender="a@test", recipient="b@test", content="forever",
        )
        tagged = reaper.tag_ephemeral(msg)
        assert "ephemeral" not in tagged.metadata


class TestExpiryResult:
    """Tests for the result model."""

    def test_summary(self) -> None:
        """Summary is human-readable."""
        r = ExpiryResult(scanned=10, expired=3, active_ephemeral=2)
        s = r.summary()
        assert "3 expired" in s
        assert "10 scanned" in s
