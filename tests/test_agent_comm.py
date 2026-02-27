"""Tests for agent-to-agent communication."""

import pytest
from unittest.mock import MagicMock, patch

from skchat.models import ChatMessage, ContentType
from skchat.agent_comm import AgentMessenger


def _mock_history():
    """Create a mock ChatHistory with working store_message."""
    history = MagicMock()
    history.store_message.return_value = "mem-001"
    history._store = MagicMock()
    history._store.list_memories.return_value = []
    history.get_thread_messages.return_value = []
    return history


class TestAgentMessenger:
    def test_create_messenger(self):
        history = _mock_history()
        messenger = AgentMessenger(
            identity="capauth:jarvis@skworld.io",
            history=history,
        )
        assert messenger.identity == "capauth:jarvis@skworld.io"
        assert not messenger.has_transport
        assert messenger.team_id is None

    def test_create_with_team(self):
        history = _mock_history()
        messenger = AgentMessenger(
            identity="capauth:jarvis@skworld.io",
            history=history,
            team_id="team-alpha",
        )
        assert messenger.team_id == "team-alpha"

    def test_send_stores_message(self):
        history = _mock_history()
        messenger = AgentMessenger(
            identity="capauth:jarvis@skworld.io",
            history=history,
        )

        result = messenger.send(
            recipient="capauth:lumina@skworld.io",
            content="Found a bug in transport.py",
        )

        assert result["message_id"]
        assert result["stored"] is True
        history.store_message.assert_called_once()

        stored_msg = history.store_message.call_args[0][0]
        assert stored_msg.sender == "capauth:jarvis@skworld.io"
        assert stored_msg.recipient == "capauth:lumina@skworld.io"
        assert stored_msg.metadata["agent_comm"] is True
        assert stored_msg.metadata["message_type"] == "text"

    def test_send_with_transport(self):
        history = _mock_history()
        transport = MagicMock()
        transport.send_message.return_value = {
            "delivered": True,
            "transport": "syncthing",
        }

        messenger = AgentMessenger(
            identity="capauth:jarvis@skworld.io",
            history=history,
            transport=transport,
        )

        result = messenger.send(
            recipient="capauth:lumina@skworld.io",
            content="Hello!",
        )

        assert result["delivered"] is True
        assert result["transport"] == "syncthing"
        transport.send_message.assert_called_once()

    def test_send_transport_failure(self):
        history = _mock_history()
        transport = MagicMock()
        transport.send_message.side_effect = ConnectionError("offline")

        messenger = AgentMessenger(
            identity="capauth:jarvis@skworld.io",
            history=history,
            transport=transport,
        )

        result = messenger.send(
            recipient="capauth:lumina@skworld.io",
            content="Hello!",
        )

        assert result["delivered"] is False
        assert "offline" in result["error"]


class TestStructuredMessages:
    def test_send_finding(self):
        history = _mock_history()
        messenger = AgentMessenger(
            identity="capauth:jarvis@skworld.io",
            history=history,
        )

        result = messenger.send_finding(
            recipient="capauth:opus@skworld.io",
            summary="Null pointer in parser",
            details="Line 42 of parser.py dereferences None",
            source_file="src/parser.py",
            severity="error",
        )

        assert result["message_id"]
        stored_msg = history.store_message.call_args[0][0]
        assert stored_msg.metadata["message_type"] == "finding"
        assert "Finding" in stored_msg.content
        assert "ERROR" in stored_msg.content
        assert stored_msg.metadata["payload"]["severity"] == "error"
        assert stored_msg.metadata["payload"]["source_file"] == "src/parser.py"

    def test_send_task_update(self):
        history = _mock_history()
        messenger = AgentMessenger(
            identity="capauth:jarvis@skworld.io",
            history=history,
        )

        result = messenger.send_task_update(
            recipient="capauth:opus@skworld.io",
            task_id="abc12345",
            status="completed",
            summary="Plugin SDK implemented and tested",
        )

        stored_msg = history.store_message.call_args[0][0]
        assert stored_msg.metadata["message_type"] == "task"
        assert stored_msg.metadata["payload"]["task_id"] == "abc12345"
        assert stored_msg.metadata["payload"]["status"] == "completed"

    def test_query_and_respond(self):
        history = _mock_history()
        messenger = AgentMessenger(
            identity="capauth:jarvis@skworld.io",
            history=history,
        )

        q_result = messenger.query(
            recipient="capauth:lumina@skworld.io",
            question="What's the status of the transport layer?",
            context={"project": "skchat"},
        )

        q_msg = history.store_message.call_args[0][0]
        assert q_msg.metadata["message_type"] == "query"
        assert "Query" in q_msg.content

        r_result = messenger.respond(
            recipient="capauth:lumina@skworld.io",
            answer="Transport layer is operational with 3 transports.",
            reply_to=q_result["message_id"],
        )

        r_msg = history.store_message.call_args[0][0]
        assert r_msg.metadata["message_type"] == "response"
        assert r_msg.reply_to == q_result["message_id"]


class TestTeamBroadcast:
    def test_broadcast_to_team(self):
        history = _mock_history()
        messenger = AgentMessenger(
            identity="capauth:jarvis@skworld.io",
            history=history,
            team_id="dev-team",
        )

        results = messenger.broadcast_team(
            content="Deploying v2.0 in 5 minutes",
            team_uris=[
                "capauth:jarvis@skworld.io",
                "capauth:lumina@skworld.io",
                "capauth:opus@skworld.io",
            ],
        )

        # Should skip self
        assert len(results) == 2
        # All should have stored=True (no transport)
        for r in results:
            assert r["stored"] is True

    def test_broadcast_empty_team(self):
        history = _mock_history()
        messenger = AgentMessenger(
            identity="capauth:jarvis@skworld.io",
            history=history,
        )

        results = messenger.broadcast_team(
            content="Hello",
            team_uris=[],
        )
        assert results == []

    def test_team_scoped_thread(self):
        history = _mock_history()
        messenger = AgentMessenger(
            identity="capauth:jarvis@skworld.io",
            history=history,
            team_id="dev-team",
        )

        messenger.send(
            recipient="capauth:lumina@skworld.io",
            content="Team message",
        )

        stored_msg = history.store_message.call_args[0][0]
        assert stored_msg.thread_id == "dev-team"
        assert stored_msg.metadata["team_id"] == "dev-team"


class TestInbox:
    def test_get_inbox_empty(self):
        history = _mock_history()
        messenger = AgentMessenger(
            identity="capauth:jarvis@skworld.io",
            history=history,
        )

        inbox = messenger.get_inbox()
        assert inbox == []

    def test_get_inbox_filters_agent_comm(self):
        history = _mock_history()

        mock_memory = MagicMock()
        mock_memory.id = "mem-001"
        mock_memory.content = "Hello from lumina"
        mock_memory.created_at = "2026-02-27T10:00:00"
        mock_memory.tags = ["skchat:message"]
        mock_memory.metadata = {
            "agent_comm": True,
            "message_type": "text",
            "sender": "capauth:lumina@skworld.io",
            "chat_message_id": "msg-001",
        }
        history._store.list_memories.return_value = [mock_memory]

        messenger = AgentMessenger(
            identity="capauth:jarvis@skworld.io",
            history=history,
        )

        inbox = messenger.get_inbox()
        assert len(inbox) == 1
        assert inbox[0]["sender"] == "capauth:lumina@skworld.io"
        assert inbox[0]["message_type"] == "text"

    def test_get_inbox_filters_by_type(self):
        history = _mock_history()

        mem_text = MagicMock()
        mem_text.id = "mem-001"
        mem_text.content = "Hello"
        mem_text.created_at = "2026-02-27T10:00:00"
        mem_text.tags = ["skchat:message"]
        mem_text.metadata = {
            "agent_comm": True,
            "message_type": "text",
            "sender": "capauth:lumina@skworld.io",
        }

        mem_finding = MagicMock()
        mem_finding.id = "mem-002"
        mem_finding.content = "Bug found"
        mem_finding.created_at = "2026-02-27T10:01:00"
        mem_finding.tags = ["skchat:message"]
        mem_finding.metadata = {
            "agent_comm": True,
            "message_type": "finding",
            "sender": "capauth:opus@skworld.io",
        }

        history._store.list_memories.return_value = [mem_text, mem_finding]

        messenger = AgentMessenger(
            identity="capauth:jarvis@skworld.io",
            history=history,
        )

        findings_only = messenger.get_inbox(message_type="finding")
        assert len(findings_only) == 1
        assert findings_only[0]["message_type"] == "finding"
