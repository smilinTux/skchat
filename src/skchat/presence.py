"""Presence indicators for SKChat — who's online, typing, or away.

Presence is ephemeral state: it doesn't persist to SKMemory.
It flows as lightweight signals over SKComm alongside chat messages.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
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
        return [
            uri
            for uri, ind in self._indicators.items()
            if ind.is_active()
        ]

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
        stale = [
            uri
            for uri, ind in self._indicators.items()
            if ind.is_stale(max_age_seconds)
        ]
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
