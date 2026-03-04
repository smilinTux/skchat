"""Tests for the SKChat transport bridge (skchat.transport.ChatTransport).

Covers:
- send_message with mocked SKComm (success + failure)
- poll_inbox with mocked envelopes
- send_and_store convenience method
- Graceful degradation when SKComm operations fail
- Direct file inbox polling (_poll_file_inbox)
- Loopback delivery when sender == receiver
"""

from __future__ import annotations

import json
from pathlib import Path
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


# ---------------------------------------------------------------------------
# File inbox polling tests
# ---------------------------------------------------------------------------


def _make_transport(tmp_path: Path, identity: str = "capauth:test@skchat") -> tuple:
    """Create a ChatTransport wired to a temp file inbox and mock history."""
    mock_skcomm = MagicMock()
    mock_skcomm.receive.return_value = []
    mock_history = MagicMock()
    mock_history.store_message.return_value = "mem-001"

    ct = ChatTransport(
        skcomm=mock_skcomm,
        history=mock_history,
        identity=identity,
    )
    # Override the file inbox root to use a tmp dir — avoids touching ~/.skcomm
    ct._file_inbox_root = tmp_path / "transport" / "file" / "inbox"
    return ct, mock_skcomm, mock_history


class TestFileInboxPoll:
    """Tests for ChatTransport._poll_file_inbox()."""

    def test_poll_file_inbox_no_dir(self, tmp_path):
        """Returns [] gracefully when the fingerprint inbox dir doesn't exist."""
        ct, _, _ = _make_transport(tmp_path)
        # Override fingerprint so we control the path
        ct._get_own_fingerprint = lambda: "TESTFP"  # type: ignore[method-assign]

        messages = ct._poll_file_inbox()

        assert messages == []

    def test_poll_file_inbox_empty_dir(self, tmp_path):
        """Returns [] when the inbox dir exists but is empty."""
        ct, _, _ = _make_transport(tmp_path)
        ct._get_own_fingerprint = lambda: "TESTFP"  # type: ignore[method-assign]

        inbox = ct._file_inbox_root / "TESTFP"  # type: ignore[attr-defined]
        inbox.mkdir(parents=True)

        assert ct._poll_file_inbox() == []

    def test_poll_file_inbox_detects_message(self, tmp_path):
        """Valid ChatMessage JSON envelope is detected, parsed, and returned."""
        ct, _, mock_history = _make_transport(tmp_path)
        ct._get_own_fingerprint = lambda: "TESTFP"  # type: ignore[method-assign]

        inbox = ct._file_inbox_root / "TESTFP"  # type: ignore[attr-defined]
        inbox.mkdir(parents=True)

        msg = ChatMessage(
            sender="capauth:alice@skworld.io",
            recipient="capauth:test@skchat",
            content="Hello from file inbox!",
        )
        envelope = {
            "skcomm_version": "1.0.0",
            "envelope_id": "aabbccdd",
            "sender": msg.sender,
            "recipient": msg.recipient,
            "payload": {"content": msg.model_dump_json(), "content_type": "text"},
        }
        (inbox / "aabbccdd.skc.json").write_text(
            json.dumps(envelope), encoding="utf-8"
        )

        messages = ct._poll_file_inbox()

        assert len(messages) == 1
        assert messages[0].content == "Hello from file inbox!"
        assert messages[0].delivery_status == DeliveryStatus.DELIVERED
        mock_history.store_message.assert_called_once()

    def test_poll_file_inbox_archives_processed_file(self, tmp_path):
        """Processed files are moved to the archive directory."""
        ct, _, _ = _make_transport(tmp_path)
        ct._get_own_fingerprint = lambda: "TESTFP"  # type: ignore[method-assign]

        inbox = ct._file_inbox_root / "TESTFP"  # type: ignore[attr-defined]
        inbox.mkdir(parents=True)

        msg = ChatMessage(
            sender="capauth:bob@skworld.io",
            recipient="capauth:test@skchat",
            content="archive me",
        )
        envelope = {
            "envelope_id": "ee112233",
            "payload": {"content": msg.model_dump_json()},
        }
        env_file = inbox / "ee112233.skc.json"
        env_file.write_text(json.dumps(envelope), encoding="utf-8")

        ct._poll_file_inbox()

        # Original file should be gone
        assert not env_file.exists()
        # Archive dir should contain it
        archive_dir = ct._file_inbox_root / "archive" / "TESTFP"  # type: ignore[attr-defined]
        assert archive_dir.exists()
        archived = list(archive_dir.glob("*.skc.json"))
        assert len(archived) == 1

    def test_poll_file_inbox_skips_dotfiles(self, tmp_path):
        """Temp files starting with '.' are not processed."""
        ct, _, mock_history = _make_transport(tmp_path)
        ct._get_own_fingerprint = lambda: "TESTFP"  # type: ignore[method-assign]

        inbox = ct._file_inbox_root / "TESTFP"  # type: ignore[attr-defined]
        inbox.mkdir(parents=True)
        (inbox / ".in_progress.skc.json").write_text("{}", encoding="utf-8")

        ct._poll_file_inbox()

        mock_history.store_message.assert_not_called()

    def test_poll_file_inbox_skips_invalid_json(self, tmp_path):
        """Invalid JSON files are archived without crashing."""
        ct, _, mock_history = _make_transport(tmp_path)
        ct._get_own_fingerprint = lambda: "TESTFP"  # type: ignore[method-assign]

        inbox = ct._file_inbox_root / "TESTFP"  # type: ignore[attr-defined]
        inbox.mkdir(parents=True)
        (inbox / "bad.skc.json").write_text("not json {{{{", encoding="utf-8")

        messages = ct._poll_file_inbox()

        assert messages == []
        mock_history.store_message.assert_not_called()
        # Original bad file should still be archived (not left behind)
        assert not (inbox / "bad.skc.json").exists()

    def test_poll_inbox_includes_file_inbox_results(self, tmp_path):
        """poll_inbox() combines SKComm envelopes + direct file inbox results."""
        ct, mock_skcomm, mock_history = _make_transport(tmp_path)
        ct._get_own_fingerprint = lambda: "TESTFP"  # type: ignore[method-assign]

        inbox = ct._file_inbox_root / "TESTFP"  # type: ignore[attr-defined]
        inbox.mkdir(parents=True)

        # Put one message in the file inbox
        msg = ChatMessage(
            sender="capauth:peer@skworld.io",
            recipient="capauth:test@skchat",
            content="via file inbox",
        )
        envelope = {
            "envelope_id": "ff998877",
            "payload": {"content": msg.model_dump_json()},
        }
        (inbox / "ff998877.skc.json").write_text(
            json.dumps(envelope), encoding="utf-8"
        )

        # SKComm receive returns nothing
        mock_skcomm.receive.return_value = []

        messages = ct.poll_inbox()

        assert len(messages) == 1
        assert messages[0].content == "via file inbox"


# ---------------------------------------------------------------------------
# Loopback send tests
# ---------------------------------------------------------------------------


class TestLoopback:
    """Tests for sender == receiver loopback delivery."""

    def test_loopback_send_does_not_call_skcomm(self, tmp_path):
        """Self-addressed messages bypass SKComm.send()."""
        ct, mock_skcomm, mock_history = _make_transport(
            tmp_path, identity="capauth:self@skworld.io"
        )
        ct._get_own_fingerprint = lambda: "SELFTEST"  # type: ignore[method-assign]

        msg = ChatMessage(
            sender="capauth:self@skworld.io",
            recipient="capauth:self@skworld.io",
            content="self-message",
        )

        result = ct.send_message(msg)

        assert result["delivered"] is True
        assert result["transport"] == "file"
        mock_skcomm.send.assert_not_called()

    def test_loopback_send_writes_to_file_inbox(self, tmp_path):
        """Self-addressed message creates a .skc.json file in own inbox dir."""
        ct, _, _ = _make_transport(
            tmp_path, identity="capauth:self@skworld.io"
        )
        ct._get_own_fingerprint = lambda: "SELFTEST"  # type: ignore[method-assign]

        msg = ChatMessage(
            sender="capauth:self@skworld.io",
            recipient="capauth:self@skworld.io",
            content="persisted via file",
        )

        ct.send_message(msg)

        inbox = ct._file_inbox_root / "SELFTEST"  # type: ignore[attr-defined]
        files = list(inbox.glob("*.skc.json"))
        assert len(files) == 1

    def test_loopback_message_is_recoverable_on_next_poll(self, tmp_path):
        """Send-then-poll roundtrip works end-to-end for loopback."""
        ct, _, mock_history = _make_transport(
            tmp_path, identity="capauth:self@skworld.io"
        )
        ct._get_own_fingerprint = lambda: "SELFTEST"  # type: ignore[method-assign]

        # Reset call count to track history writes from poll separately
        msg = ChatMessage(
            sender="capauth:self@skworld.io",
            recipient="capauth:self@skworld.io",
            content="round-trip test",
        )
        ct.send_message(msg)

        # Now poll — should find the written file
        mock_history.reset_mock()
        messages = ct._poll_file_inbox()

        assert len(messages) == 1
        assert messages[0].content == "round-trip test"
        mock_history.store_message.assert_called_once()

    def test_get_own_fingerprint_fallback_slug(self, tmp_path):
        """Falls back to identity slug when no ~/.skcomm/config.yml fingerprint."""
        ct, _, _ = _make_transport(
            tmp_path, identity="capauth:opus@skworld.io"
        )
        # Point config to a non-existent path to force fallback
        with patch("builtins.open", side_effect=FileNotFoundError):
            fp = ct._get_own_fingerprint()

        assert "@" not in fp  # slug sanitizes @ → _at_
        assert fp  # non-empty
