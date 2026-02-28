"""Tests for SKChat MCP server tools.

Covers:
- send_message tool
- check_inbox tool
- search_messages tool
- create_group tool
- group_send tool
- group_members tool
- group_add_member tool
- list_threads tool
- get_thread tool
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from skchat.mcp_server import (
    _handle_send_message,
    _handle_check_inbox,
    _handle_search_messages,
    _handle_create_group,
    _handle_group_send,
    _handle_group_members,
    _handle_group_add_member,
    _handle_list_threads,
    _handle_get_thread,
    _groups,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_result(result: list) -> dict:
    """Parse the JSON result from a tool handler."""
    return json.loads(result[0].text)


def _mock_messenger():
    """Create a mock AgentMessenger."""
    messenger = MagicMock()
    messenger.send.return_value = {
        "message_id": "msg-001",
        "delivered": True,
        "transport": "local",
    }
    messenger.get_inbox.return_value = [
        {
            "id": "msg-002",
            "sender": "capauth:lumina@skworld.io",
            "content": "Hello!",
            "timestamp": "2026-02-27T12:00:00+00:00",
            "message_type": "text",
            "delivery_status": "delivered",
        }
    ]
    return messenger


def _mock_history():
    """Create a mock ChatHistory."""
    history = MagicMock()
    history.search_messages.return_value = [
        {
            "id": "msg-003",
            "sender": "capauth:opus@skworld.io",
            "content": "Found a bug in transport.py",
            "timestamp": "2026-02-27T10:00:00+00:00",
        }
    ]
    history.list_threads.return_value = [
        {
            "id": "thread-001",
            "title": "Bug Discussion",
            "participants": ["capauth:opus@skworld.io", "capauth:lumina@skworld.io"],
            "message_count": 5,
            "created_at": "2026-02-27T09:00:00+00:00",
            "updated_at": "2026-02-27T12:00:00+00:00",
        }
    ]
    history.get_thread_messages.return_value = [
        {
            "id": "msg-004",
            "sender": "capauth:opus@skworld.io",
            "content": "Looking into this now.",
            "timestamp": "2026-02-27T09:05:00+00:00",
        }
    ]
    history.store_message.return_value = "mem-001"
    return history


# ---------------------------------------------------------------------------
# 1. send_message
# ---------------------------------------------------------------------------


class TestSendMessage:
    """Tests for the send_message tool."""

    @pytest.mark.asyncio
    async def test_send_success(self):
        messenger = _mock_messenger()
        with patch("skchat.mcp_server._get_messenger", return_value=messenger):
            result = await _handle_send_message({
                "recipient": "capauth:lumina@skworld.io",
                "content": "Hello Lumina!",
            })

        data = _parse_result(result)
        assert data["sent"] is True
        assert data["recipient"] == "capauth:lumina@skworld.io"
        messenger.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_missing_recipient(self):
        result = await _handle_send_message({"content": "Hello"})
        data = _parse_result(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_send_missing_content(self):
        result = await _handle_send_message({"recipient": "capauth:x@y"})
        data = _parse_result(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_send_with_thread(self):
        messenger = _mock_messenger()
        with patch("skchat.mcp_server._get_messenger", return_value=messenger):
            result = await _handle_send_message({
                "recipient": "capauth:lumina@skworld.io",
                "content": "Reply in thread",
                "thread_id": "thread-001",
                "message_type": "response",
            })

        data = _parse_result(result)
        assert data["sent"] is True
        assert data["thread_id"] == "thread-001"


# ---------------------------------------------------------------------------
# 2. check_inbox
# ---------------------------------------------------------------------------


class TestCheckInbox:
    """Tests for the check_inbox tool."""

    @pytest.mark.asyncio
    async def test_inbox_returns_messages(self):
        messenger = _mock_messenger()
        with patch("skchat.mcp_server._get_messenger", return_value=messenger):
            result = await _handle_check_inbox({})

        data = _parse_result(result)
        assert data["count"] == 1
        assert data["messages"][0]["sender"] == "capauth:lumina@skworld.io"

    @pytest.mark.asyncio
    async def test_inbox_with_limit(self):
        messenger = _mock_messenger()
        with patch("skchat.mcp_server._get_messenger", return_value=messenger):
            result = await _handle_check_inbox({"limit": 5})

        messenger.get_inbox.assert_called_with(limit=5, message_type=None)

    @pytest.mark.asyncio
    async def test_inbox_with_type_filter(self):
        messenger = _mock_messenger()
        with patch("skchat.mcp_server._get_messenger", return_value=messenger):
            result = await _handle_check_inbox({"message_type": "finding"})

        messenger.get_inbox.assert_called_with(limit=20, message_type="finding")


# ---------------------------------------------------------------------------
# 3. search_messages
# ---------------------------------------------------------------------------


class TestSearchMessages:
    """Tests for the search_messages tool."""

    @pytest.mark.asyncio
    async def test_search_returns_results(self):
        history = _mock_history()
        with patch("skchat.mcp_server._get_history", return_value=history):
            result = await _handle_search_messages({"query": "bug"})

        data = _parse_result(result)
        assert data["query"] == "bug"
        assert data["count"] == 1

    @pytest.mark.asyncio
    async def test_search_requires_query(self):
        result = await _handle_search_messages({})
        data = _parse_result(result)
        assert "error" in data


# ---------------------------------------------------------------------------
# 4. create_group
# ---------------------------------------------------------------------------


class TestCreateGroup:
    """Tests for the create_group tool."""

    @pytest.mark.asyncio
    async def test_create_group_success(self):
        with patch("skchat.mcp_server._get_identity", return_value="capauth:opus@skworld.io"):
            result = await _handle_create_group({
                "name": "Test Group",
                "description": "A test group",
                "members": [
                    {"identity": "capauth:lumina@skworld.io", "role": "member"},
                    {"identity": "capauth:jarvis@skworld.io", "participant_type": "agent"},
                ],
            })

        data = _parse_result(result)
        assert data["name"] == "Test Group"
        assert data["member_count"] >= 1  # Creator + members
        assert data["group_id"] in _groups

        # Cleanup
        del _groups[data["group_id"]]

    @pytest.mark.asyncio
    async def test_create_group_requires_name(self):
        result = await _handle_create_group({})
        data = _parse_result(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_create_group_empty_members(self):
        with patch("skchat.mcp_server._get_identity", return_value="capauth:opus@skworld.io"):
            result = await _handle_create_group({"name": "Solo Group"})

        data = _parse_result(result)
        assert data["name"] == "Solo Group"
        assert data["member_count"] >= 1  # At least the creator

        del _groups[data["group_id"]]


# ---------------------------------------------------------------------------
# 5. group_send
# ---------------------------------------------------------------------------


class TestGroupSend:
    """Tests for the group_send tool."""

    @pytest.mark.asyncio
    async def test_group_send_success(self):
        # Create a group first
        from skchat.group import GroupChat
        group = GroupChat.create(
            name="Send Test",
            creator_uri="capauth:opus@skworld.io",
        )
        _groups[group.id] = group

        history = _mock_history()
        with patch("skchat.mcp_server._get_identity", return_value="capauth:opus@skworld.io"):
            with patch("skchat.mcp_server._get_history", return_value=history):
                result = await _handle_group_send({
                    "group_id": group.id,
                    "content": "Hello group!",
                })

        data = _parse_result(result)
        assert data["sent"] is True
        assert data["group_name"] == "Send Test"

        del _groups[group.id]

    @pytest.mark.asyncio
    async def test_group_send_missing_group(self):
        result = await _handle_group_send({
            "group_id": "nonexistent",
            "content": "Hello",
        })
        data = _parse_result(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_group_send_missing_content(self):
        result = await _handle_group_send({"group_id": "x"})
        data = _parse_result(result)
        assert "error" in data


# ---------------------------------------------------------------------------
# 6. group_members
# ---------------------------------------------------------------------------


class TestGroupMembers:
    """Tests for the group_members tool."""

    @pytest.mark.asyncio
    async def test_group_members_success(self):
        from skchat.group import GroupChat
        group = GroupChat.create(
            name="Members Test",
            creator_uri="capauth:opus@skworld.io",
        )
        _groups[group.id] = group

        result = await _handle_group_members({"group_id": group.id})
        data = _parse_result(result)
        assert data["group_name"] == "Members Test"
        assert data["member_count"] >= 1

        del _groups[group.id]

    @pytest.mark.asyncio
    async def test_group_members_not_found(self):
        result = await _handle_group_members({"group_id": "nope"})
        data = _parse_result(result)
        assert "error" in data


# ---------------------------------------------------------------------------
# 7. group_add_member
# ---------------------------------------------------------------------------


class TestGroupAddMember:
    """Tests for the group_add_member tool."""

    @pytest.mark.asyncio
    async def test_add_member_success(self):
        from skchat.group import GroupChat
        group = GroupChat.create(
            name="Add Test",
            creator_uri="capauth:opus@skworld.io",
        )
        _groups[group.id] = group

        result = await _handle_group_add_member({
            "group_id": group.id,
            "identity": "capauth:lumina@skworld.io",
            "role": "member",
            "participant_type": "agent",
        })

        data = _parse_result(result)
        assert data["added"] is True
        assert data["member_count"] >= 2

        del _groups[group.id]

    @pytest.mark.asyncio
    async def test_add_member_missing_group(self):
        result = await _handle_group_add_member({
            "group_id": "nope",
            "identity": "capauth:x@y",
        })
        data = _parse_result(result)
        assert "error" in data


# ---------------------------------------------------------------------------
# 8. list_threads
# ---------------------------------------------------------------------------


class TestListThreads:
    """Tests for the list_threads tool."""

    @pytest.mark.asyncio
    async def test_list_threads_returns_data(self):
        history = _mock_history()
        with patch("skchat.mcp_server._get_history", return_value=history):
            result = await _handle_list_threads({})

        data = _parse_result(result)
        assert data["count"] == 1
        assert data["threads"][0]["title"] == "Bug Discussion"

    @pytest.mark.asyncio
    async def test_list_threads_with_limit(self):
        history = _mock_history()
        with patch("skchat.mcp_server._get_history", return_value=history):
            result = await _handle_list_threads({"limit": 5})

        history.list_threads.assert_called_with(limit=5)


# ---------------------------------------------------------------------------
# 9. get_thread
# ---------------------------------------------------------------------------


class TestGetThread:
    """Tests for the get_thread tool."""

    @pytest.mark.asyncio
    async def test_get_thread_returns_messages(self):
        history = _mock_history()
        with patch("skchat.mcp_server._get_history", return_value=history):
            result = await _handle_get_thread({"thread_id": "thread-001"})

        data = _parse_result(result)
        assert data["thread_id"] == "thread-001"
        assert data["count"] == 1

    @pytest.mark.asyncio
    async def test_get_thread_requires_id(self):
        result = await _handle_get_thread({})
        data = _parse_result(result)
        assert "error" in data
