"""Presence indicators for SKChat — who's online, typing, or away.

Presence is ephemeral state: it doesn't persist to SKMemory.
It flows as lightweight signals over SKComm alongside chat messages.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class PresenceState(str, Enum):
    """Possible presence states for a participant."""

    ONLINE = "online"
    OFFLINE = "offline"
    AWAY = "away"
    DND = "do-not-disturb"
    TYPING = "typing"


class PresenceIndicator(BaseModel):
    """A presence signal from a chat participant.

    Presence indicators are fire-and-forget: they carry the latest
    state of a participant without requiring acknowledgment.
    Stale indicators are garbage-collected by the UI layer.

    Attributes:
        id: UUID v4 indicator identifier.
        identity_uri: CapAuth identity URI of the participant.
        state: Current presence state.
        thread_id: Optional thread context (typing in a specific thread).
        timestamp: When this indicator was generated.
        custom_status: Optional freeform status text (e.g., "In a meeting").
        expires_at: When this indicator should be considered stale.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    identity_uri: str = Field(description="CapAuth identity URI")
    state: PresenceState = Field(default=PresenceState.ONLINE)
    thread_id: Optional[str] = Field(default=None)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    custom_status: Optional[str] = Field(default=None)
    expires_at: Optional[datetime] = Field(default=None)

    def is_stale(self, max_age_seconds: int = 120) -> bool:
        """Check if this presence indicator is too old to trust.

        Args:
            max_age_seconds: Maximum age before considered stale.

        Returns:
            bool: True if the indicator has expired or is too old.
        """
        if self.expires_at and datetime.now(timezone.utc) > self.expires_at:
            return True
        age = (datetime.now(timezone.utc) - self.timestamp).total_seconds()
        return age > max_age_seconds

    def is_active(self) -> bool:
        """Check if the participant is actively available.

        Returns:
            bool: True if online or typing (and not stale).
        """
        if self.is_stale():
            return False
        return self.state in (PresenceState.ONLINE, PresenceState.TYPING)


class PresenceTracker:
    """Tracks presence state for all known participants.

    Maintains an in-memory map of identity URIs to their latest
    presence indicator. Thread-safe for single-threaded async use.

    The tracker does not persist state — presence is ephemeral by design.
    """

    def __init__(self) -> None:
        self._indicators: dict[str, PresenceIndicator] = {}

    def update(self, indicator: PresenceIndicator) -> None:
        """Update or add a presence indicator for a participant.

        Only applies the update if the indicator is newer than
        the currently tracked one.

        Args:
            indicator: The new presence indicator.
        """
        uri = indicator.identity_uri
        existing = self._indicators.get(uri)
        if existing is None or indicator.timestamp >= existing.timestamp:
            self._indicators[uri] = indicator

    def get(self, identity_uri: str) -> Optional[PresenceIndicator]:
        """Get the latest presence indicator for a participant.

        Args:
            identity_uri: CapAuth identity URI.

        Returns:
            Optional[PresenceIndicator]: Latest indicator, or None if unknown.
        """
        return self._indicators.get(identity_uri)

    def get_state(self, identity_uri: str) -> PresenceState:
        """Get the current presence state for a participant.

        Returns OFFLINE if the participant is unknown or their
        indicator is stale.

        Args:
            identity_uri: CapAuth identity URI.

        Returns:
            PresenceState: Current state, defaulting to OFFLINE.
        """
        indicator = self._indicators.get(identity_uri)
        if indicator is None or indicator.is_stale():
            return PresenceState.OFFLINE
        return indicator.state

    def who_is_online(self) -> list[str]:
        """List all participants currently active (online or typing).

        Returns:
            list[str]: Identity URIs of active participants.
        """
        return [uri for uri, ind in self._indicators.items() if ind.is_active()]

    def who_is_typing(self, thread_id: Optional[str] = None) -> list[str]:
        """List participants currently typing, optionally in a specific thread.

        Args:
            thread_id: If provided, only return typers in this thread.

        Returns:
            list[str]: Identity URIs of typing participants.
        """
        result = []
        for uri, ind in self._indicators.items():
            if ind.state != PresenceState.TYPING or ind.is_stale():
                continue
            if thread_id is not None and ind.thread_id != thread_id:
                continue
            result.append(uri)
        return result

    def remove(self, identity_uri: str) -> bool:
        """Remove a participant from tracking.

        Args:
            identity_uri: CapAuth identity URI to remove.

        Returns:
            bool: True if the participant was being tracked.
        """
        return self._indicators.pop(identity_uri, None) is not None

    def prune_stale(self, max_age_seconds: int = 120) -> int:
        """Remove all stale presence indicators.

        Args:
            max_age_seconds: Maximum age before considered stale.

        Returns:
            int: Number of stale indicators removed.
        """
        stale = [uri for uri, ind in self._indicators.items() if ind.is_stale(max_age_seconds)]
        for uri in stale:
            del self._indicators[uri]
        return len(stale)

    @property
    def tracked_count(self) -> int:
        """Number of participants currently being tracked.

        Returns:
            int: Count of tracked participants.
        """
        return len(self._indicators)


class PresenceCache:
    """File-backed presence cache — who was seen, when, and in what state.

    Unlike PresenceTracker (in-memory only), PresenceCache persists
    presence records to a JSON file so the CLI and MCP tools can query
    presence without running inside the daemon process.

    Records are keyed by identity URI. Each entry stores the last-seen
    timestamp and state. The file is reloaded on every read call so the
    cache stays fresh across processes.

    Typing state is tracked in-memory only (5 s TTL); it is too ephemeral
    to write to disk.

    Attributes:
        CACHE_FILE: Default path (~/.skchat/presence_cache.json).
    """

    CACHE_FILE = Path("~/.skchat/presence_cache.json")
    _TYPING_TTL: float = 5.0  # seconds before typing indicator auto-expires

    def __init__(self, cache_file: Optional[Path] = None) -> None:
        self._path = (cache_file or self.CACHE_FILE).expanduser()
        self._data: dict[str, dict] = {}
        self._typing: dict[str, float] = {}  # identity_uri -> monotonic timestamp
        self._load()

    def _load(self) -> None:
        """Reload cache from disk (no-op if file absent)."""
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text())
                self._data = raw if isinstance(raw, dict) else {}
            except Exception:
                self._data = {}

    def _save(self) -> None:
        """Atomically persist cache to disk via temp-file rename."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data))
        tmp.rename(self._path)

    def record(
        self,
        identity_uri: str,
        state: PresenceState,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """Record or update presence for a peer.

        Args:
            identity_uri: CapAuth identity URI.
            state: Current presence state.
            timestamp: When the presence was recorded (default: now).
        """
        ts = (timestamp or datetime.now(timezone.utc)).isoformat()
        self._data[identity_uri] = {"state": state.value, "timestamp": ts}
        self._save()

    def get_entry(self, identity_uri: str) -> Optional[dict]:
        """Get the raw cache entry for a peer.

        Args:
            identity_uri: CapAuth identity URI.

        Returns:
            dict with 'state' and 'timestamp' keys, or None if unknown.
        """
        self._load()
        return self._data.get(identity_uri)

    def get_all(self) -> dict[str, dict]:
        """Return all cached presence entries.

        Returns:
            dict mapping identity URI -> {'state': str, 'timestamp': str}.
        """
        self._load()
        return dict(self._data)

    def get_online(self, max_age: int = 300) -> list[str]:
        """List peers seen within the last *max_age* seconds.

        Only peers whose state is not OFFLINE and whose last-seen
        timestamp is within *max_age* seconds are returned.

        Args:
            max_age: Max age in seconds (default: 300 = 5 minutes).

        Returns:
            list[str]: Identity URIs of recently-seen peers.
        """
        self._load()
        now = datetime.now(timezone.utc)
        result = []
        for uri, entry in self._data.items():
            try:
                ts = datetime.fromisoformat(entry["timestamp"])
                age = (now - ts).total_seconds()
                state = entry.get("state", "")
                if age <= max_age and state != PresenceState.OFFLINE.value:
                    result.append(uri)
            except Exception:
                pass
        return result

    def get_status(self, identity_uri: str) -> str:
        """Get human-readable status for a peer.

        Thresholds: online (<2 min), away (<10 min), offline (>=10 min or unknown).

        Args:
            identity_uri: CapAuth identity URI.

        Returns:
            str: "online", "away", or "offline".
        """
        self._load()
        entry = self._data.get(identity_uri)
        if not entry:
            return "offline"
        try:
            ts = datetime.fromisoformat(entry["timestamp"])
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            state = entry.get("state", "")
            if state == PresenceState.OFFLINE.value:
                return "offline"
            if age <= 120:
                return "online"
            if age <= 600:
                return "away"
            return "offline"
        except Exception:
            return "offline"

    # ── Typing indicator methods ───────────────────────────────────────────

    def set_typing(self, identity: str, is_typing: bool) -> None:
        """Record or clear a typing indicator for a peer (in-memory, 5 s TTL).

        When *is_typing* is True, stamps the current monotonic time and
        schedules an auto-clear timer so stale indicators are removed even
        if the peer never sends a stop signal.

        Args:
            identity: CapAuth identity URI of the peer.
            is_typing: True to start the indicator, False to clear it.
        """
        if not is_typing:
            self._typing.pop(identity, None)
            return
        self._typing[identity] = time.monotonic()
        t = threading.Timer(self._TYPING_TTL, self._clear_typing, args=(identity,))
        t.daemon = True
        t.start()

    def _clear_typing(self, identity: str) -> None:
        """Remove typing state for *identity* after TTL expires."""
        self._typing.pop(identity, None)

    def is_typing(self, identity: str) -> bool:
        """Return True if *identity* sent a typing signal within the last 5 seconds.

        Args:
            identity: CapAuth identity URI.

        Returns:
            bool: True if the peer is currently typing.
        """
        ts = self._typing.get(identity)
        if ts is None:
            return False
        return (time.monotonic() - ts) <= self._TYPING_TTL

    def get_typing_peers(self) -> list[str]:
        """Return identity URIs of peers whose typing signal is still fresh.

        Returns:
            list[str]: Peers whose typing indicator was received within the last 5 s.
        """
        now = time.monotonic()
        return [uri for uri, ts in list(self._typing.items()) if (now - ts) <= self._TYPING_TTL]
