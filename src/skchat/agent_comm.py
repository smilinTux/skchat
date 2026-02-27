"""Agent-to-agent communication — programmatic messaging for sovereign agents.

Provides AgentMessenger, a high-level API for agents to exchange messages,
share findings, and coordinate work within teams. Built on top of
ChatTransport (SKComm) and ChatHistory (SKMemory).

Unlike the CLI-oriented send/receive flow, AgentMessenger is designed for
programmatic use: agents import it and call methods directly, without
needing Click or a terminal.

Usage:
    messenger = AgentMessenger.from_identity()
    messenger.send("capauth:lumina@skworld.io", "Found a bug in transport.py")
    messages = messenger.receive()
    messenger.broadcast_team("Deploying v2.0 in 5 minutes", team_uris=[...])
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .history import ChatHistory
from .models import ChatMessage, ContentType, DeliveryStatus

logger = logging.getLogger("skchat.agent_comm")


class AgentMessenger:
    """High-level messaging API for agent-to-agent communication.

    Wraps ChatTransport for sending and ChatHistory for persistence.
    Provides team-scoped channels, structured payloads, and coordination
    primitives that agents need for multi-agent collaboration.

    Args:
        identity: CapAuth identity URI of this agent.
        history: ChatHistory instance for persistence.
        transport: Optional ChatTransport for P2P delivery.
        team_id: Optional team identifier for scoping messages.
    """

    AGENT_TAG = "skchat:agent_comm"
    TEAM_TAG_PREFIX = "skchat:team:"

    def __init__(
        self,
        identity: str,
        history: ChatHistory,
        transport: Optional[object] = None,
        team_id: Optional[str] = None,
    ) -> None:
        self._identity = identity
        self._history = history
        self._transport = transport
        self._team_id = team_id

    @classmethod
    def from_identity(
        cls,
        identity: Optional[str] = None,
        team_id: Optional[str] = None,
    ) -> "AgentMessenger":
        """Create an AgentMessenger from the local sovereign identity.

        Resolves identity from CapAuth, initializes history and transport.

        Args:
            identity: Override identity URI. Auto-detected if None.
            team_id: Optional team scope.

        Returns:
            AgentMessenger: Ready to send/receive.
        """
        if identity is None:
            from .identity_bridge import get_sovereign_identity
            identity = get_sovereign_identity()

        history = ChatHistory.from_config()
        transport = cls._try_init_transport(history, identity)

        return cls(
            identity=identity,
            history=history,
            transport=transport,
            team_id=team_id,
        )

    @staticmethod
    def _try_init_transport(history: ChatHistory, identity: str) -> Optional[object]:
        """Try to initialize ChatTransport backed by SKComm.

        Returns None if SKComm is not available.
        """
        try:
            from skcomm import SKComm
            from .transport import ChatTransport

            skcomm = SKComm.from_config()
            return ChatTransport(
                skcomm=skcomm,
                history=history,
                identity=identity,
            )
        except Exception:
            return None

    @property
    def identity(self) -> str:
        """This agent's CapAuth identity URI."""
        return self._identity

    @property
    def team_id(self) -> Optional[str]:
        """Current team scope, if any."""
        return self._team_id

    @property
    def has_transport(self) -> bool:
        """Whether P2P transport is available."""
        return self._transport is not None

    def send(
        self,
        recipient: str,
        content: str,
        message_type: str = "text",
        thread_id: Optional[str] = None,
        reply_to: Optional[str] = None,
        payload: Optional[dict] = None,
        ttl: Optional[int] = None,
    ) -> dict:
        """Send a message to another agent.

        Args:
            recipient: CapAuth identity URI of the target agent.
            content: Message text.
            message_type: Structured type (text, finding, task, query, response).
            thread_id: Optional thread for conversation grouping.
            reply_to: Optional message ID this replies to.
            payload: Optional structured data attached to the message.
            ttl: Optional seconds until auto-delete.

        Returns:
            dict: Delivery result with 'delivered', 'message_id', etc.
        """
        metadata: dict[str, Any] = {
            "agent_comm": True,
            "message_type": message_type,
            "sender_agent": self._identity,
        }
        if self._team_id:
            metadata["team_id"] = self._team_id
        if payload:
            metadata["payload"] = payload

        msg = ChatMessage(
            sender=self._identity,
            recipient=recipient,
            content=content,
            content_type=ContentType.MARKDOWN,
            thread_id=thread_id or self._team_id,
            reply_to=reply_to,
            ttl=ttl,
            metadata=metadata,
        )

        # Store locally
        self._history.store_message(msg)

        # Deliver via transport if available
        if self._transport:
            try:
                result = self._transport.send_message(msg)
                return {
                    "delivered": result.get("delivered", False),
                    "message_id": msg.id,
                    "transport": result.get("transport"),
                }
            except Exception as exc:
                logger.warning("Transport delivery failed: %s", exc)
                return {
                    "delivered": False,
                    "message_id": msg.id,
                    "error": str(exc),
                }

        return {
            "delivered": False,
            "message_id": msg.id,
            "error": "no transport available",
            "stored": True,
        }

    def send_finding(
        self,
        recipient: str,
        summary: str,
        details: Optional[str] = None,
        source_file: Optional[str] = None,
        severity: str = "info",
        thread_id: Optional[str] = None,
    ) -> dict:
        """Send a structured finding to another agent.

        Findings are agent-originated observations: bugs found, patterns
        detected, optimization opportunities, security issues.

        Args:
            recipient: Target agent URI.
            summary: Short summary of the finding.
            details: Full details/context.
            source_file: File path where the finding originates.
            severity: info, warning, error, critical.
            thread_id: Optional thread scope.

        Returns:
            dict: Delivery result.
        """
        payload = {
            "severity": severity,
            "source_file": source_file,
            "details": details,
        }

        content = f"**Finding** [{severity.upper()}]: {summary}"
        if source_file:
            content += f"\nSource: `{source_file}`"
        if details:
            content += f"\n\n{details}"

        return self.send(
            recipient=recipient,
            content=content,
            message_type="finding",
            payload=payload,
            thread_id=thread_id,
        )

    def send_task_update(
        self,
        recipient: str,
        task_id: str,
        status: str,
        summary: str,
        thread_id: Optional[str] = None,
    ) -> dict:
        """Send a task status update to another agent.

        Used for coordination: "I finished X", "I'm blocked on Y",
        "I started working on Z".

        Args:
            recipient: Target agent URI.
            task_id: Coordination board task ID.
            status: Task status (started, progress, blocked, completed).
            summary: What happened.
            thread_id: Optional thread scope.

        Returns:
            dict: Delivery result.
        """
        payload = {
            "task_id": task_id,
            "status": status,
        }

        content = f"**Task Update** [{task_id[:8]}] {status}: {summary}"

        return self.send(
            recipient=recipient,
            content=content,
            message_type="task",
            payload=payload,
            thread_id=thread_id,
        )

    def query(
        self,
        recipient: str,
        question: str,
        context: Optional[dict] = None,
        thread_id: Optional[str] = None,
    ) -> dict:
        """Send a query to another agent requesting information.

        Args:
            recipient: Target agent URI.
            question: The question to ask.
            context: Optional context dict for the query.
            thread_id: Optional thread scope.

        Returns:
            dict: Delivery result.
        """
        payload = {"context": context or {}}

        return self.send(
            recipient=recipient,
            content=f"**Query**: {question}",
            message_type="query",
            payload=payload,
            thread_id=thread_id,
        )

    def respond(
        self,
        recipient: str,
        answer: str,
        reply_to: str,
        payload: Optional[dict] = None,
        thread_id: Optional[str] = None,
    ) -> dict:
        """Send a response to a previous query.

        Args:
            recipient: Target agent URI.
            answer: The answer text.
            reply_to: Message ID of the original query.
            payload: Optional structured response data.
            thread_id: Optional thread scope.

        Returns:
            dict: Delivery result.
        """
        return self.send(
            recipient=recipient,
            content=answer,
            message_type="response",
            reply_to=reply_to,
            payload=payload,
            thread_id=thread_id,
        )

    def broadcast_team(
        self,
        content: str,
        team_uris: Optional[list[str]] = None,
        message_type: str = "text",
        payload: Optional[dict] = None,
    ) -> list[dict]:
        """Broadcast a message to all agents in a team.

        Args:
            content: Message text.
            team_uris: List of agent URIs. If None, uses known team members.
            message_type: Structured type.
            payload: Optional structured data.

        Returns:
            list[dict]: Delivery results for each recipient.
        """
        if team_uris is None:
            team_uris = self._discover_team_members()

        results = []
        for uri in team_uris:
            if uri == self._identity:
                continue
            result = self.send(
                recipient=uri,
                content=content,
                message_type=message_type,
                payload=payload,
                thread_id=self._team_id,
            )
            results.append(result)

        if results:
            delivered = sum(1 for r in results if r.get("delivered"))
            logger.info(
                "Broadcast to %d agents (%d delivered)",
                len(results), delivered,
            )

        return results

    def receive(self, limit: int = 50) -> list[dict]:
        """Receive pending agent messages from transport.

        Polls SKComm for incoming messages and returns those tagged
        as agent communications.

        Args:
            limit: Maximum messages to return.

        Returns:
            list[dict]: Received agent messages as dicts.
        """
        if self._transport:
            try:
                self._transport.poll_inbox()
            except Exception as exc:
                logger.warning("Poll failed: %s", exc)

        # Retrieve agent messages from history
        return self.get_inbox(limit=limit)

    def get_inbox(self, limit: int = 50, message_type: Optional[str] = None) -> list[dict]:
        """Get agent messages from local history.

        Args:
            limit: Maximum messages to return.
            message_type: Filter by message type (finding, task, query, etc.).

        Returns:
            list[dict]: Agent messages, newest first.
        """
        tag = f"skchat:recipient:{self._identity}"
        memories = self._history._store.list_memories(
            tags=["skchat:message", tag],
            limit=limit * 2,
        )

        results = []
        for m in memories:
            if not m.metadata.get("agent_comm"):
                continue
            if message_type and m.metadata.get("message_type") != message_type:
                continue

            results.append({
                "memory_id": m.id,
                "message_id": m.metadata.get("chat_message_id"),
                "sender": m.metadata.get("sender"),
                "content": m.content,
                "message_type": m.metadata.get("message_type", "text"),
                "payload": m.metadata.get("payload"),
                "team_id": m.metadata.get("team_id"),
                "thread_id": m.metadata.get("thread_id"),
                "reply_to": m.metadata.get("reply_to"),
                "timestamp": m.created_at,
            })

        results.sort(key=lambda d: d.get("timestamp", ""), reverse=True)
        return results[:limit]

    def get_team_messages(self, limit: int = 50) -> list[dict]:
        """Get all messages in the current team channel.

        Args:
            limit: Maximum messages to return.

        Returns:
            list[dict]: Team messages, newest first.
        """
        if not self._team_id:
            return []

        return self._history.get_thread_messages(self._team_id, limit=limit)

    def _discover_team_members(self) -> list[str]:
        """Discover team members from the peer registry.

        Returns:
            list[str]: Known agent identity URIs.
        """
        try:
            from .identity_bridge import _list_peers
            peers = _list_peers()
            return [p["identity_uri"] for p in peers if p.get("identity_uri")]
        except Exception:
            return []
