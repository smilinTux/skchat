"""SKChat reactions and annotations -- social signaling for messages.

Reactions let participants (human and AI) respond to messages with
emoji or text annotations without creating a full reply. Reactions
are lightweight, transportable via SKComm, and stored alongside
messages in ChatHistory.

The ReactionManager handles add/remove, deduplication, aggregation,
and serialization for transport.

Usage:
    manager = ReactionManager()
    manager.add_reaction("msg-123", "thumbsup", "capauth:alice@skworld.io")
    summary = manager.summarize("msg-123")
    payload = manager.to_sync_payload("msg-123")  # for SKComm
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from .models import ChatMessage, Reaction

logger = logging.getLogger("skchat.reactions")


class ReactionEvent(BaseModel):
    """A reaction event for sync over SKComm.

    This is the transportable form of a reaction -- sent to peers
    so they can update their local reaction state.

    Attributes:
        event_id: Unique event identifier.
        message_id: ID of the message being reacted to.
        emoji: Reaction emoji or short text.
        sender: CapAuth identity URI of the reactor.
        action: 'add' or 'remove'.
        timestamp: When the event occurred.
    """

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    message_id: str
    emoji: str
    sender: str
    action: str = "add"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReactionSummary(BaseModel):
    """Aggregated reaction summary for a message.

    Attributes:
        message_id: The message these reactions belong to.
        reactions: Dict of emoji -> list of sender URIs.
        total_count: Total number of reactions.
        unique_reactors: Number of unique people who reacted.
    """

    message_id: str
    reactions: dict[str, list[str]] = Field(default_factory=dict)
    total_count: int = 0
    unique_reactors: int = 0

    def display(self) -> str:
        """Format reactions for terminal display.

        Returns:
            str: Formatted string like 'thumbsup(3) heart(2)'.
        """
        if not self.reactions:
            return ""
        parts = [f"{emoji}({len(senders)})" for emoji, senders in self.reactions.items()]
        return " ".join(parts)


class ReactionManager:
    """Manages reactions across all messages.

    Stores reactions in memory with deduplication. Provides
    aggregation, serialization for transport, and sync from
    incoming ReactionEvents.
    """

    def __init__(self) -> None:
        self._reactions: dict[str, list[Reaction]] = {}
        self._event_log: list[ReactionEvent] = []
        self._seen_events: set[str] = set()

    def add_reaction(
        self,
        message_id: str,
        emoji: str,
        sender: str,
    ) -> bool:
        """Add a reaction to a message.

        Deduplicates: same sender + same emoji on the same message
        is only counted once.

        Args:
            message_id: The message to react to.
            emoji: Reaction emoji or short text.
            sender: CapAuth identity URI of the reactor.

        Returns:
            bool: True if the reaction was added (not a duplicate).
        """
        reactions = self._reactions.setdefault(message_id, [])

        if any(r.emoji == emoji and r.sender == sender for r in reactions):
            return False

        reactions.append(Reaction(emoji=emoji, sender=sender))

        self._event_log.append(ReactionEvent(
            message_id=message_id, emoji=emoji, sender=sender, action="add",
        ))

        logger.debug("Reaction added: %s on %s by %s", emoji, message_id[:8], sender)
        return True

    def remove_reaction(
        self,
        message_id: str,
        emoji: str,
        sender: str,
    ) -> bool:
        """Remove a reaction from a message.

        Args:
            message_id: The message to remove the reaction from.
            emoji: The emoji to remove.
            sender: The sender who added the reaction.

        Returns:
            bool: True if the reaction was found and removed.
        """
        reactions = self._reactions.get(message_id, [])
        before = len(reactions)
        self._reactions[message_id] = [
            r for r in reactions if not (r.emoji == emoji and r.sender == sender)
        ]
        removed = len(self._reactions[message_id]) < before

        if removed:
            self._event_log.append(ReactionEvent(
                message_id=message_id, emoji=emoji, sender=sender, action="remove",
            ))

        return removed

    def toggle_reaction(
        self,
        message_id: str,
        emoji: str,
        sender: str,
    ) -> bool:
        """Toggle a reaction: add if not present, remove if present.

        Args:
            message_id: The message to toggle on.
            emoji: The emoji to toggle.
            sender: The sender.

        Returns:
            bool: True if the reaction is now present, False if removed.
        """
        if self.has_reaction(message_id, emoji, sender):
            self.remove_reaction(message_id, emoji, sender)
            return False
        else:
            self.add_reaction(message_id, emoji, sender)
            return True

    def has_reaction(
        self,
        message_id: str,
        emoji: str,
        sender: str,
    ) -> bool:
        """Check if a specific reaction exists.

        Args:
            message_id: The message ID.
            emoji: The emoji.
            sender: The sender.

        Returns:
            bool: True if the reaction exists.
        """
        reactions = self._reactions.get(message_id, [])
        return any(r.emoji == emoji and r.sender == sender for r in reactions)

    def get_reactions(self, message_id: str) -> list[Reaction]:
        """Get all reactions for a message.

        Args:
            message_id: The message ID.

        Returns:
            list[Reaction]: All reactions on this message.
        """
        return list(self._reactions.get(message_id, []))

    def summarize(self, message_id: str) -> ReactionSummary:
        """Aggregate reactions for a message into a summary.

        Args:
            message_id: The message ID.

        Returns:
            ReactionSummary: Aggregated reaction data.
        """
        reactions = self._reactions.get(message_id, [])
        grouped: dict[str, list[str]] = {}
        unique: set[str] = set()

        for r in reactions:
            grouped.setdefault(r.emoji, []).append(r.sender)
            unique.add(r.sender)

        return ReactionSummary(
            message_id=message_id,
            reactions=grouped,
            total_count=len(reactions),
            unique_reactors=len(unique),
        )

    def apply_event(self, event: ReactionEvent) -> bool:
        """Apply an incoming ReactionEvent from a peer.

        Used during sync to update local state from remote events.
        Deduplicates by event_id.

        Args:
            event: The incoming reaction event.

        Returns:
            bool: True if the event was applied (not a duplicate).
        """
        if event.event_id in self._seen_events:
            return False

        self._seen_events.add(event.event_id)

        if event.action == "add":
            return self.add_reaction(event.message_id, event.emoji, event.sender)
        elif event.action == "remove":
            return self.remove_reaction(event.message_id, event.emoji, event.sender)

        return False

    def pending_events(self, since: Optional[datetime] = None) -> list[ReactionEvent]:
        """Get reaction events for sync to peers.

        Args:
            since: Only return events after this time.

        Returns:
            list[ReactionEvent]: Events ready for transport.
        """
        if since is None:
            return list(self._event_log)
        return [e for e in self._event_log if e.timestamp >= since]

    def message_count(self) -> int:
        """Number of messages that have reactions.

        Returns:
            int: Count of messages with at least one reaction.
        """
        return sum(1 for rs in self._reactions.values() if rs)

    def total_reactions(self) -> int:
        """Total number of reactions across all messages.

        Returns:
            int: Total reaction count.
        """
        return sum(len(rs) for rs in self._reactions.values())

    def top_reacted(self, limit: int = 10) -> list[tuple[str, int]]:
        """Get the most-reacted messages.

        Args:
            limit: Max results.

        Returns:
            list[tuple[str, int]]: (message_id, reaction_count) sorted by count.
        """
        counts = [
            (mid, len(rs)) for mid, rs in self._reactions.items() if rs
        ]
        counts.sort(key=lambda x: x[1], reverse=True)
        return counts[:limit]
