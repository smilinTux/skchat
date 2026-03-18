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
- webrtc_status tool
- initiate_call tool
- accept_call tool
- send_file_p2p tool
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from skchat.mcp_server import (
    _groups,
    _handle_accept_call,
    _handle_check_inbox,
    _handle_create_group,
    _handle_get_thread,
    _handle_group_add_member,
    _handle_group_members,
    _handle_group_send,
    _handle_initiate_call,
    _handle_list_threads,
    _handle_search_messages,
    _handle_send_file_p2p,
    _handle_send_message,
    _handle_webrtc_status,
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
            result = await _handle_send_message(
                {
                    "recipient": "capauth:lumina@skworld.io",
                    "content": "Hello Lumina!",
                }
            )

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
            result = await _handle_send_message(
                {
                    "recipient": "capauth:lumina@skworld.io",
                    "content": "Reply in thread",
                    "thread_id": "thread-001",
                    "message_type": "response",
                }
            )

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
            result = await _handle_create_group(
                {
                    "name": "Test Group",
                    "description": "A test group",
                    "members": [
                        {"identity": "capauth:lumina@skworld.io", "role": "member"},
                        {"identity": "capauth:jarvis@skworld.io", "participant_type": "agent"},
                    ],
                }
            )

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
                result = await _handle_group_send(
                    {
                        "group_id": group.id,
                        "content": "Hello group!",
                    }
                )

        data = _parse_result(result)
        assert data["sent"] is True
        assert data["group_name"] == "Send Test"

        del _groups[group.id]

    @pytest.mark.asyncio
    async def test_group_send_missing_group(self):
        result = await _handle_group_send(
            {
                "group_id": "nonexistent",
                "content": "Hello",
            }
        )
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

        result = await _handle_group_add_member(
            {
                "group_id": group.id,
                "identity": "capauth:lumina@skworld.io",
                "role": "member",
                "participant_type": "agent",
            }
        )

        data = _parse_result(result)
        assert data["added"] is True
        assert data["member_count"] >= 2

        del _groups[group.id]

    @pytest.mark.asyncio
    async def test_add_member_missing_group(self):
        result = await _handle_group_add_member(
            {
                "group_id": "nope",
                "identity": "capauth:x@y",
            }
        )
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


# ---------------------------------------------------------------------------
# 10. webrtc_status
# ---------------------------------------------------------------------------


PEER_FP = "AAAA9306410CF8CD5E393D6DEC31663B95230684"


def _mock_webrtc_transport(
    running: bool = True,
    signaling_connected: bool = True,
    peers: dict | None = None,
    inbox_size: int = 0,
) -> MagicMock:
    """Create a mock WebRTCTransport for MCP tool tests."""
    from queue import Queue

    from skcomm.transport import HealthStatus, TransportStatus

    transport = MagicMock()
    transport._running = running
    transport._signaling_connected = signaling_connected
    transport._signaling_url = "ws://localhost:9384/webrtc/ws"
    transport._peers = peers or {}
    transport._peers_lock = __import__("threading").Lock()
    transport.is_available.return_value = running and signaling_connected
    q = Queue()
    for _ in range(inbox_size):
        q.put(b"msg")
    transport._inbox = q

    status = (
        TransportStatus.AVAILABLE
        if (running and signaling_connected)
        else (TransportStatus.DEGRADED if running else TransportStatus.UNAVAILABLE)
    )
    transport.health_check.return_value = HealthStatus(
        transport_name="webrtc",
        status=status,
        error=None if status == TransportStatus.AVAILABLE else "not connected",
    )
    return transport


class TestWebRTCStatus:
    """Tests for the webrtc_status MCP tool."""

    @pytest.mark.asyncio
    async def test_no_transport_returns_unavailable(self):
        """Expected: available=False when WebRTC transport not configured."""
        with patch("skchat.mcp_server._get_webrtc_transport", return_value=None):
            result = await _handle_webrtc_status({})
        data = _parse_result(result)
        assert data["available"] is False
        assert "reason" in data

    @pytest.mark.asyncio
    async def test_transport_available(self):
        """Expected: returns transport status when configured and running."""
        transport = _mock_webrtc_transport()
        with patch("skchat.mcp_server._get_webrtc_transport", return_value=transport):
            result = await _handle_webrtc_status({})
        data = _parse_result(result)
        assert data["available"] is True
        assert data["running"] is True
        assert data["signaling_connected"] is True

    @pytest.mark.asyncio
    async def test_transport_running_but_no_signaling(self):
        """Expected: running=True, available=False when signaling disconnected."""
        transport = _mock_webrtc_transport(signaling_connected=False)
        with patch("skchat.mcp_server._get_webrtc_transport", return_value=transport):
            result = await _handle_webrtc_status({})
        data = _parse_result(result)
        assert data["running"] is True
        assert data["available"] is False

    @pytest.mark.asyncio
    async def test_status_includes_inbox_pending(self):
        """Expected: inbox_pending count included in status."""
        transport = _mock_webrtc_transport(inbox_size=3)
        with patch("skchat.mcp_server._get_webrtc_transport", return_value=transport):
            result = await _handle_webrtc_status({})
        data = _parse_result(result)
        assert data["inbox_pending"] == 3

    @pytest.mark.asyncio
    async def test_status_includes_peer_info(self):
        """Expected: active_peers includes info for connected peers."""
        pytest.importorskip("aiortc")
        from skcomm.transports.webrtc import PeerConnection

        mock_pc = MagicMock()
        peer = PeerConnection(
            peer_fingerprint=PEER_FP,
            pc=mock_pc,
            connected=True,
            negotiating=False,
        )
        transport = _mock_webrtc_transport(peers={PEER_FP: peer})
        with patch("skchat.mcp_server._get_webrtc_transport", return_value=transport):
            result = await _handle_webrtc_status({})
        data = _parse_result(result)
        peer_key = PEER_FP[:8]
        assert peer_key in data["active_peers"]
        assert data["active_peers"][peer_key]["connected"] is True


# ---------------------------------------------------------------------------
# 11. initiate_call
# ---------------------------------------------------------------------------


class TestInitiateCall:
    """Tests for the initiate_call MCP tool."""

    @pytest.mark.asyncio
    async def test_missing_peer_returns_error(self):
        """Expected: error when peer argument is missing."""
        result = await _handle_initiate_call({})
        data = _parse_result(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_no_transport_returns_error(self):
        """Expected: error when WebRTC transport not configured."""
        with patch("skchat.mcp_server._get_webrtc_transport", return_value=None):
            result = await _handle_initiate_call({"peer": PEER_FP})
        data = _parse_result(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_initiate_schedules_offer(self):
        """Expected: _schedule_offer called with peer fingerprint."""
        transport = _mock_webrtc_transport()
        transport._running = True
        with patch("skchat.mcp_server._get_webrtc_transport", return_value=transport):
            result = await _handle_initiate_call({"peer": PEER_FP})
        data = _parse_result(result)
        assert data["initiated"] is True
        assert data["peer"] == PEER_FP
        transport._schedule_offer.assert_called_once_with(PEER_FP)

    @pytest.mark.asyncio
    async def test_starts_transport_if_not_running(self):
        """Expected: transport.start() called when transport is stopped."""
        transport = _mock_webrtc_transport(running=False, signaling_connected=False)
        transport._running = False
        with patch("skchat.mcp_server._get_webrtc_transport", return_value=transport):
            result = await _handle_initiate_call({"peer": PEER_FP})
        transport.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_initiate_response_includes_signaling_url(self):
        """Expected: response includes the signaling URL."""
        transport = _mock_webrtc_transport()
        with patch("skchat.mcp_server._get_webrtc_transport", return_value=transport):
            result = await _handle_initiate_call({"peer": PEER_FP})
        data = _parse_result(result)
        assert "signaling_url" in data


# ---------------------------------------------------------------------------
# 12. accept_call
# ---------------------------------------------------------------------------


class TestAcceptCall:
    """Tests for the accept_call MCP tool."""

    @pytest.mark.asyncio
    async def test_missing_peer_returns_error(self):
        """Expected: error when peer argument is missing."""
        result = await _handle_accept_call({})
        data = _parse_result(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_no_transport_returns_error(self):
        """Expected: error when WebRTC transport not configured."""
        with patch("skchat.mcp_server._get_webrtc_transport", return_value=None):
            result = await _handle_accept_call({"peer": PEER_FP})
        data = _parse_result(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_already_connected_returns_connected_status(self):
        """Expected: status=already_connected when peer is already connected."""
        pytest.importorskip("aiortc")
        from skcomm.transports.webrtc import PeerConnection

        peer = PeerConnection(peer_fingerprint=PEER_FP, pc=MagicMock(), connected=True)
        transport = _mock_webrtc_transport(peers={PEER_FP: peer})
        with patch("skchat.mcp_server._get_webrtc_transport", return_value=transport):
            result = await _handle_accept_call({"peer": PEER_FP})
        data = _parse_result(result)
        assert data["accepted"] is True
        assert data["status"] == "already_connected"

    @pytest.mark.asyncio
    async def test_not_connected_initiates_negotiation(self):
        """Expected: _schedule_offer called when not yet connected."""
        transport = _mock_webrtc_transport()
        with patch("skchat.mcp_server._get_webrtc_transport", return_value=transport):
            result = await _handle_accept_call({"peer": PEER_FP})
        data = _parse_result(result)
        assert data["accepted"] is True
        assert data["status"] == "negotiating"
        transport._schedule_offer.assert_called_once_with(PEER_FP)

    @pytest.mark.asyncio
    async def test_starts_transport_if_stopped(self):
        """Expected: transport.start() called when not running."""
        transport = _mock_webrtc_transport(running=False, signaling_connected=False)
        transport._running = False
        with patch("skchat.mcp_server._get_webrtc_transport", return_value=transport):
            await _handle_accept_call({"peer": PEER_FP})
        transport.start.assert_called_once()


# ---------------------------------------------------------------------------
# 13. send_file_p2p
# ---------------------------------------------------------------------------


class TestSendFileP2P:
    """Tests for the send_file_p2p MCP tool."""

    @pytest.mark.asyncio
    async def test_missing_peer_returns_error(self):
        """Expected: error when peer is not provided."""
        result = await _handle_send_file_p2p({"file_path": "/tmp/test.txt"})
        data = _parse_result(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_missing_file_path_returns_error(self):
        """Expected: error when file_path is not provided."""
        result = await _handle_send_file_p2p({"peer": PEER_FP})
        data = _parse_result(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_file_not_found_returns_error(self):
        """Expected: error when the specified file does not exist."""
        result = await _handle_send_file_p2p(
            {
                "peer": PEER_FP,
                "file_path": "/nonexistent/path/to/file.txt",
            }
        )
        data = _parse_result(result)
        assert "error" in data
        assert "not found" in data["error"].lower() or "File not found" in data["error"]

    @pytest.mark.asyncio
    async def test_directory_path_returns_error(self, tmp_path):
        """Expected: error when file_path points to a directory."""
        result = await _handle_send_file_p2p(
            {
                "peer": PEER_FP,
                "file_path": str(tmp_path),
            }
        )
        data = _parse_result(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_skcomm_fallback_on_no_webrtc(self, tmp_path):
        """Expected: falls back to SKComm when no WebRTC transport available."""
        test_file = tmp_path / "test.txt"
        test_file.write_bytes(b"hello p2p world")

        mock_transfer = MagicMock()
        mock_transfer.transfer_id = "xfer-001"
        mock_transfer.total_chunks = 1
        mock_transfer.sha256 = "abc123"
        mock_transfer.encrypted_key = "enckey"

        mock_chunk = MagicMock()
        mock_chunk.to_json.return_value = '{"chunk": "data"}'

        mock_sender = MagicMock()
        mock_sender.prepare.return_value = mock_transfer
        mock_sender.chunks.return_value = [mock_chunk]

        mock_comm = MagicMock()

        with patch("skchat.mcp_server._get_webrtc_transport", return_value=None):
            with patch("skchat.mcp_server._get_identity", return_value="capauth:opus@skworld.io"):
                with patch("skchat.files.FileSender", return_value=mock_sender):
                    with patch("skcomm.SKComm.from_config", return_value=mock_comm):
                        result = await _handle_send_file_p2p(
                            {
                                "peer": PEER_FP,
                                "file_path": str(test_file),
                            }
                        )

        data = _parse_result(result)
        assert data["sent"] is True
        assert data["transport"] == "skcomm-chunked"
        assert data["transfer_id"] == "xfer-001"

    @pytest.mark.asyncio
    async def test_webrtc_direct_send_when_connected(self, tmp_path):
        """Expected: uses WebRTC data channel when peer is connected."""
        pytest.importorskip("aiortc")
        test_file = tmp_path / "direct.txt"
        test_file.write_bytes(b"direct transfer")

        from skcomm.transports.webrtc import PeerConnection

        mock_channel = MagicMock()
        peer = PeerConnection(
            peer_fingerprint=PEER_FP,
            pc=MagicMock(),
            channel=mock_channel,
            connected=True,
        )
        transport = _mock_webrtc_transport(peers={PEER_FP: peer})

        mock_transfer = MagicMock()
        mock_transfer.transfer_id = "xfer-002"
        mock_transfer.total_chunks = 1
        mock_transfer.sha256 = "def456"
        mock_transfer.encrypted_key = "enckey2"

        mock_chunk = MagicMock()
        mock_chunk.to_json.return_value = '{"chunk": "data2"}'

        mock_sender = MagicMock()
        mock_sender.prepare.return_value = mock_transfer
        mock_sender.chunks.return_value = [mock_chunk]

        mock_future = MagicMock()
        mock_future.result.return_value = None

        with patch("skchat.mcp_server._get_webrtc_transport", return_value=transport):
            with patch("skchat.mcp_server._get_identity", return_value="capauth:opus@skworld.io"):
                with patch("skchat.files.FileSender", return_value=mock_sender):
                    with patch("asyncio.run_coroutine_threadsafe", return_value=mock_future):
                        result = await _handle_send_file_p2p(
                            {
                                "peer": PEER_FP,
                                "file_path": str(test_file),
                                "description": "test transfer",
                            }
                        )

        data = _parse_result(result)
        assert data["sent"] is True
        assert data["transport"] == "webrtc-direct"
        assert data["transfer_id"] == "xfer-002"
