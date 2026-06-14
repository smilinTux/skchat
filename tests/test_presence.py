"""Tests for SKChat presence — PresenceIndicator, PresenceTracker, PresenceCache."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skchat.presence import (
    PresenceCache,
    PresenceIndicator,
    PresenceState,
    PresenceTracker,
    presence_status,
)


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
        tracker.update(
            PresenceIndicator(
                identity_uri="capauth:alice@skworld.io",
                state=PresenceState.ONLINE,
            )
        )
        tracker.update(
            PresenceIndicator(
                identity_uri="capauth:bob@skworld.io",
                state=PresenceState.OFFLINE,
            )
        )
        tracker.update(
            PresenceIndicator(
                identity_uri="capauth:lumina@skworld.io",
                state=PresenceState.TYPING,
            )
        )

        online = tracker.who_is_online()
        assert "capauth:alice@skworld.io" in online
        assert "capauth:lumina@skworld.io" in online
        assert "capauth:bob@skworld.io" not in online

    def test_who_is_typing(self) -> None:
        """who_is_typing filters by thread when specified."""
        tracker = PresenceTracker()
        tracker.update(
            PresenceIndicator(
                identity_uri="capauth:alice@skworld.io",
                state=PresenceState.TYPING,
                thread_id="thread-1",
            )
        )
        tracker.update(
            PresenceIndicator(
                identity_uri="capauth:bob@skworld.io",
                state=PresenceState.TYPING,
                thread_id="thread-2",
            )
        )

        all_typing = tracker.who_is_typing()
        assert len(all_typing) == 2

        thread_typing = tracker.who_is_typing(thread_id="thread-1")
        assert len(thread_typing) == 1
        assert "capauth:alice@skworld.io" in thread_typing

    def test_remove(self) -> None:
        """Removing a tracked participant returns True."""
        tracker = PresenceTracker()
        tracker.update(
            PresenceIndicator(
                identity_uri="capauth:alice@skworld.io",
                state=PresenceState.ONLINE,
            )
        )
        assert tracker.remove("capauth:alice@skworld.io") is True
        assert tracker.get("capauth:alice@skworld.io") is None

    def test_remove_nonexistent(self) -> None:
        """Removing an untracked participant returns False."""
        tracker = PresenceTracker()
        assert tracker.remove("capauth:nobody@test") is False

    def test_prune_stale(self) -> None:
        """prune_stale removes old indicators."""
        tracker = PresenceTracker()
        tracker.update(
            PresenceIndicator(
                identity_uri="capauth:stale@skworld.io",
                state=PresenceState.ONLINE,
                timestamp=datetime.now(timezone.utc) - timedelta(seconds=300),
            )
        )
        tracker.update(
            PresenceIndicator(
                identity_uri="capauth:fresh@skworld.io",
                state=PresenceState.ONLINE,
            )
        )

        removed = tracker.prune_stale()
        assert removed == 1
        assert tracker.tracked_count == 1

    def test_tracked_count(self) -> None:
        """tracked_count reflects total tracked participants."""
        tracker = PresenceTracker()
        assert tracker.tracked_count == 0

        for i in range(5):
            tracker.update(
                PresenceIndicator(
                    identity_uri=f"capauth:user{i}@test",
                    state=PresenceState.ONLINE,
                )
            )
        assert tracker.tracked_count == 5


class TestPresenceCacheTyping:
    """Tests for PresenceCache typing indicator methods."""

    def test_set_typing(self, tmp_path: Path) -> None:
        """set_typing marks a peer as currently typing."""
        pc = PresenceCache(cache_file=tmp_path / "presence_cache.json")
        pc.set_typing("capauth:lumina@skworld.io", True)
        assert pc.is_typing("capauth:lumina@skworld.io") is True

    def test_clear_typing(self, tmp_path: Path) -> None:
        """set_typing(False) immediately clears the indicator."""
        pc = PresenceCache(cache_file=tmp_path / "presence_cache.json")
        pc.set_typing("capauth:lumina@skworld.io", True)
        pc.set_typing("capauth:lumina@skworld.io", False)
        assert pc.is_typing("capauth:lumina@skworld.io") is False

    def test_typing_expires(self, tmp_path: Path) -> None:
        """Typing indicator reports False once the 5 s TTL has elapsed."""
        pc = PresenceCache(cache_file=tmp_path / "presence_cache.json")
        pc.set_typing("capauth:lumina@skworld.io", True)
        # Backdate the timestamp past the TTL
        pc._typing["capauth:lumina@skworld.io"] = time.monotonic() - 6.0
        assert pc.is_typing("capauth:lumina@skworld.io") is False

    def test_get_typing_peers(self, tmp_path: Path) -> None:
        """get_typing_peers returns all currently-typing peers."""
        pc = PresenceCache(cache_file=tmp_path / "presence_cache.json")
        pc.set_typing("capauth:lumina@skworld.io", True)
        pc.set_typing("capauth:chef@skworld.io", True)
        peers = pc.get_typing_peers()
        assert "capauth:lumina@skworld.io" in peers
        assert "capauth:chef@skworld.io" in peers
        assert len(peers) == 2

    def test_get_typing_peers_excludes_expired(self, tmp_path: Path) -> None:
        """get_typing_peers excludes peers whose indicators have expired."""
        pc = PresenceCache(cache_file=tmp_path / "presence_cache.json")
        pc.set_typing("capauth:lumina@skworld.io", True)
        pc.set_typing("capauth:chef@skworld.io", True)
        # Expire lumina's indicator
        pc._typing["capauth:lumina@skworld.io"] = time.monotonic() - 6.0
        peers = pc.get_typing_peers()
        assert "capauth:lumina@skworld.io" not in peers
        assert "capauth:chef@skworld.io" in peers

    def test_unknown_peer_not_typing(self, tmp_path: Path) -> None:
        """is_typing returns False for an unknown peer."""
        pc = PresenceCache(cache_file=tmp_path / "presence_cache.json")
        assert pc.is_typing("capauth:ghost@skworld.io") is False

    def test_get_typing_peers_empty_by_default(self, tmp_path: Path) -> None:
        """get_typing_peers returns empty list when no peers are typing."""
        pc = PresenceCache(cache_file=tmp_path / "presence_cache.json")
        assert pc.get_typing_peers() == []


class TestPresenceStatus:
    """Tests for the pure presence_status() threshold function."""

    def test_recent_is_online(self) -> None:
        """A last-seen within the online window is online."""
        now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
        seen = now - timedelta(seconds=30)
        assert presence_status(seen, now) == "online"

    def test_online_boundary_inclusive(self) -> None:
        """Exactly online_within seconds old is still online."""
        now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
        seen = now - timedelta(seconds=120)
        assert presence_status(seen, now) == "online"

    def test_middle_window_is_away(self) -> None:
        """A last-seen between the online and away thresholds is away."""
        now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
        seen = now - timedelta(seconds=300)
        assert presence_status(seen, now) == "away"

    def test_away_boundary_inclusive(self) -> None:
        """Exactly away_within seconds old is still away."""
        now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
        seen = now - timedelta(seconds=600)
        assert presence_status(seen, now) == "away"

    def test_stale_is_offline(self) -> None:
        """A last-seen past the away threshold is offline."""
        now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
        seen = now - timedelta(seconds=601)
        assert presence_status(seen, now) == "offline"

    def test_missing_last_seen_is_offline(self) -> None:
        """A None last-seen is offline."""
        now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
        assert presence_status(None, now) == "offline"

    def test_custom_thresholds(self) -> None:
        """online_within / away_within are honored when overridden."""
        now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
        seen = now - timedelta(seconds=45)
        assert presence_status(seen, now, online_within=30, away_within=60) == "away"
        assert presence_status(seen, now, online_within=60, away_within=120) == "online"

    def test_default_now_uses_wall_clock(self) -> None:
        """Omitting *now* defaults to the current UTC time."""
        seen = datetime.now(timezone.utc) - timedelta(seconds=5)
        assert presence_status(seen) == "online"


class TestPresenceCacheStatus:
    """Tests for PresenceCache.get_status threshold behavior."""

    def _seed(self, pc: PresenceCache, uri: str, age_s: float, state: PresenceState) -> None:
        ts = datetime.now(timezone.utc) - timedelta(seconds=age_s)
        pc.record(uri, state, timestamp=ts)

    def test_unknown_peer_offline(self, tmp_path: Path) -> None:
        pc = PresenceCache(cache_file=tmp_path / "presence_cache.json")
        assert pc.get_status("capauth:ghost@skworld.io") == "offline"

    def test_recent_online(self, tmp_path: Path) -> None:
        pc = PresenceCache(cache_file=tmp_path / "presence_cache.json")
        self._seed(pc, "capauth:a@skworld.io", 10, PresenceState.ONLINE)
        assert pc.get_status("capauth:a@skworld.io") == "online"

    def test_middle_away(self, tmp_path: Path) -> None:
        pc = PresenceCache(cache_file=tmp_path / "presence_cache.json")
        self._seed(pc, "capauth:a@skworld.io", 300, PresenceState.ONLINE)
        assert pc.get_status("capauth:a@skworld.io") == "away"

    def test_stale_offline(self, tmp_path: Path) -> None:
        pc = PresenceCache(cache_file=tmp_path / "presence_cache.json")
        self._seed(pc, "capauth:a@skworld.io", 1200, PresenceState.ONLINE)
        assert pc.get_status("capauth:a@skworld.io") == "offline"

    def test_explicit_offline_state_overrides_recency(self, tmp_path: Path) -> None:
        """A recent record whose state is OFFLINE reports offline."""
        pc = PresenceCache(cache_file=tmp_path / "presence_cache.json")
        self._seed(pc, "capauth:a@skworld.io", 5, PresenceState.OFFLINE)
        assert pc.get_status("capauth:a@skworld.io") == "offline"


# ---------------------------------------------------------------------------
# QA additions — PresenceCache disk persistence (the cross-process contract)
# ---------------------------------------------------------------------------


class TestPresenceCachePersistence:
    """PresenceCache is the bridge between the daemon and out-of-process CLI/MCP.
    Records written by one instance must be visible to a fresh instance."""

    def test_record_persists_across_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "presence_cache.json"
        PresenceCache(cache_file=path).record(
            "capauth:lumina@skworld.io", PresenceState.ONLINE
        )
        # A brand-new instance (simulating the CLI process) reads it back.
        fresh = PresenceCache(cache_file=path)
        entry = fresh.get_entry("capauth:lumina@skworld.io")
        assert entry is not None
        assert entry["state"] == "online"

    def test_get_all_returns_copy(self, tmp_path: Path) -> None:
        pc = PresenceCache(cache_file=tmp_path / "presence_cache.json")
        pc.record("capauth:a@skworld.io", PresenceState.ONLINE)
        snap = pc.get_all()
        snap.clear()
        # Mutating the returned dict must not wipe the cache.
        assert pc.get_entry("capauth:a@skworld.io") is not None

    def test_get_online_filters_by_age(self, tmp_path: Path) -> None:
        pc = PresenceCache(cache_file=tmp_path / "presence_cache.json")
        recent = datetime.now(timezone.utc) - timedelta(seconds=10)
        old = datetime.now(timezone.utc) - timedelta(seconds=999)
        pc.record("capauth:fresh@skworld.io", PresenceState.ONLINE, timestamp=recent)
        pc.record("capauth:stale@skworld.io", PresenceState.ONLINE, timestamp=old)
        online = pc.get_online(max_age=300)
        assert "capauth:fresh@skworld.io" in online
        assert "capauth:stale@skworld.io" not in online

    def test_get_online_excludes_offline_state(self, tmp_path: Path) -> None:
        pc = PresenceCache(cache_file=tmp_path / "presence_cache.json")
        recent = datetime.now(timezone.utc) - timedelta(seconds=5)
        pc.record("capauth:gone@skworld.io", PresenceState.OFFLINE, timestamp=recent)
        assert "capauth:gone@skworld.io" not in pc.get_online()

    def test_corrupt_cache_file_degrades_gracefully(self, tmp_path: Path) -> None:
        """A garbage cache file must not crash the loader — it resets to empty."""
        path = tmp_path / "presence_cache.json"
        path.write_text("{not valid json", encoding="utf-8")
        pc = PresenceCache(cache_file=path)
        assert pc.get_all() == {}
        # And it can still record cleanly afterward.
        pc.record("capauth:a@skworld.io", PresenceState.ONLINE)
        assert pc.get_entry("capauth:a@skworld.io") is not None

    def test_get_status_handles_corrupt_timestamp(self, tmp_path: Path) -> None:
        """A record with a non-ISO timestamp falls back to 'offline'."""
        path = tmp_path / "presence_cache.json"
        path.write_text(
            '{"capauth:a@skworld.io": {"state": "online", "timestamp": "garbage"}}',
            encoding="utf-8",
        )
        pc = PresenceCache(cache_file=path)
        assert pc.get_status("capauth:a@skworld.io") == "offline"
