"""Tests for the SKChat transport bridge (skchat.transport.ChatTransport).

Covers:
- send_message with mocked SKComm (success + failure)
- poll_inbox with mocked envelopes
- send_and_store convenience method
- Graceful degradation when SKComm operations fail
"""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from skchat.models import ChatMessage, ContentType, DeliveryStatus
from skchat.transport import ChatTransport


@pytest.fixture
def mock_skcomm():
    """Create a mock SKComm instance."""
    comm = MagicMock()
    comm.send.return_value = MagicMock(
        delivered=True,
        successful_transport="syncthing",
    )
    comm.receive.return_value = []
    return comm


@pytest.fixture
def mock_history():
    """Create a mock ChatHistory."""
    history = MagicMock()
    history.store_message.return_value = "mem-123"
    return history


@pytest.fixture
def transport(mock_skcomm, mock_history):
    """Create a ChatTransport with mocked dependencies."""
    return ChatTransport(
        skcomm=mock_skcomm,
        history=mock_history,
        identity="capauth:test@skchat",
    )


class TestSendMessage:
    """Tests for ChatTransport.send_message()."""

    def test_send_success(self, transport, mock_skcomm, mock_history):
        """Message is sent via SKComm and stored in history."""
        msg = ChatMessage(
            sender="capauth:test@skchat",
            recipient="capauth:lumina@skworld",
            content="Hello Lumina!",
        )

        result = transport.send_message(msg)

        assert result["delivered"] is True
        assert result["recipient"] == "capauth:lumina@skworld"
        mock_skcomm.send.assert_called_once()
        mock_history.store_message.assert_called_once()

    def test_send_failure_stores_as_failed(self, transport, mock_skcomm, mock_history):
        """Failed delivery still stores the message with FAILED status."""
        mock_skcomm.send.return_value = MagicMock(
            delivered=False,
            successful_transport=None,
        )

        msg = ChatMessage(
            sender="capauth:test@skchat",
            recipient="capauth:nobody@nowhere",
            content="This should fail",
        )

        result = transport.send_message(msg)

        assert result["delivered"] is False
        mock_history.store_message.assert_called_once()
        stored_msg = mock_history.store_message.call_args[0][0]
        assert stored_msg.delivery_status == DeliveryStatus.FAILED

    def test_send_exception_returns_error(self, transport, mock_skcomm, mock_history):
        """SKComm exception is caught and reported gracefully."""
        mock_skcomm.send.side_effect = ConnectionError("Transport unreachable")

        msg = ChatMessage(
            sender="capauth:test@skchat",
            recipient="capauth:lumina@skworld",
            content="Oops",
        )

        result = transport.send_message(msg)

        assert result["delivered"] is False
        assert "error" in result
        assert "Transport unreachable" in result["error"]


class TestPollInbox:
    """Tests for ChatTransport.poll_inbox()."""

    def test_poll_empty_inbox(self, transport, mock_skcomm):
        """Empty inbox returns empty list."""
        mock_skcomm.receive.return_value = []

        messages = transport.poll_inbox()

        assert messages == []

    def test_poll_with_message(self, transport, mock_skcomm, mock_history):
        """Valid envelope is parsed and stored as ChatMessage."""
        msg_data = ChatMessage(
            sender="capauth:opus@smilintux",
            recipient="capauth:test@skchat",
            content="Hello from Opus!",
        )

        envelope = MagicMock()
        envelope.payload.content = msg_data.model_dump_json()
        mock_skcomm.receive.return_value = [envelope]

        messages = transport.poll_inbox()

        assert len(messages) == 1
        assert messages[0].content == "Hello from Opus!"
        assert messages[0].delivery_status == DeliveryStatus.DELIVERED
        mock_history.store_message.assert_called_once()

    def test_poll_skips_invalid_envelope(self, transport, mock_skcomm):
        """Invalid envelope payloads are silently skipped."""
        envelope = MagicMock()
        envelope.payload.content = "not valid json {{"
        mock_skcomm.receive.return_value = [envelope]

        messages = transport.poll_inbox()

        assert messages == []

    def test_poll_receive_failure(self, transport, mock_skcomm):
        """SKComm receive failure returns empty list."""
        mock_skcomm.receive.side_effect = RuntimeError("Network error")

        messages = transport.poll_inbox()

        assert messages == []


class TestSendAndStore:
    """Tests for ChatTransport.send_and_store() convenience method."""

    def test_compose_and_deliver(self, transport, mock_skcomm, mock_history):
        """send_and_store composes a ChatMessage and delivers it."""
        result = transport.send_and_store(
            recipient="capauth:lumina@skworld",
            content="Quick message",
            thread_id="thread-abc",
        )

        assert result["delivered"] is True
        mock_skcomm.send.assert_called_once()

        call_kwargs = mock_skcomm.send.call_args
        assert "capauth:lumina@skworld" in str(call_kwargs)
