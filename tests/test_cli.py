"""Tests for SKChat CLI — Click commands via CliRunner."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from skchat.cli import main


@pytest.fixture()
def runner() -> CliRunner:
    """Create a Click CliRunner.

    Returns:
        CliRunner: Isolated CLI test runner.
    """
    return CliRunner()


@pytest.fixture()
def mock_history():
    """Create a mock ChatHistory with canned responses.

    Returns:
        MagicMock: A mock ChatHistory instance.
    """
    history = MagicMock()
    history.store_message.return_value = "mem-test-123"
    history.message_count.return_value = 42
    history.list_threads.return_value = [
        {
            "thread_id": "thread-abc",
            "title": "Dev Chat",
            "participants": ["capauth:alice@test", "capauth:bob@test"],
            "message_count": 10,
            "parent_thread_id": None,
        },
    ]
    history.get_thread_messages.return_value = [
        {
            "memory_id": "mem-1",
            "chat_message_id": "msg-1",
            "sender": "capauth:alice@test",
            "recipient": "capauth:bob@test",
            "content": "Hello from the thread",
            "content_type": "text/markdown",
            "thread_id": "thread-abc",
            "reply_to": None,
            "delivery_status": "pending",
            "timestamp": "2026-02-23T12:00:00",
            "tags": ["skchat", "skchat:message"],
        },
    ]
    history.get_conversation.return_value = [
        {
            "memory_id": "mem-2",
            "chat_message_id": "msg-2",
            "sender": "capauth:bob@test",
            "recipient": "capauth:local@skchat",
            "content": "Hey there!",
            "content_type": "text/markdown",
            "thread_id": None,
            "reply_to": None,
            "delivery_status": "delivered",
            "timestamp": "2026-02-23T13:00:00",
            "tags": ["skchat", "skchat:message"],
        },
    ]
    history.search_messages.return_value = [
        {
            "memory_id": "mem-3",
            "chat_message_id": "msg-3",
            "sender": "capauth:alice@test",
            "recipient": "capauth:bob@test",
            "content": "The quantum upgrade is ready",
            "content_type": "text/markdown",
            "thread_id": None,
            "reply_to": None,
            "delivery_status": "delivered",
            "timestamp": "2026-02-23T14:00:00",
            "tags": ["skchat", "skchat:message"],
        },
    ]
    return history


class TestCLIVersion:
    """Tests for the top-level CLI group."""

    def test_version(self, runner: CliRunner) -> None:
        """Happy path: --version prints the version string."""
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "skchat" in result.output
        assert "0.1.0" in result.output

    def test_help(self, runner: CliRunner) -> None:
        """Happy path: --help shows command list."""
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "send" in result.output
        assert "inbox" in result.output
        assert "history" in result.output
        assert "threads" in result.output


class TestSendCommand:
    """Tests for the 'skchat send' command."""

    @patch("skchat.cli._try_deliver", return_value={"delivered": False, "error": "no transport", "transport": None})
    @patch("skchat.cli._get_history")
    @patch("skchat.cli._get_identity", return_value="capauth:local@skchat")
    def test_send_basic(
        self,
        mock_id: MagicMock,
        mock_hist_fn: MagicMock,
        mock_deliver: MagicMock,
        mock_history: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Happy path: send a basic message."""
        mock_hist_fn.return_value = mock_history

        result = runner.invoke(main, ["send", "capauth:bob@test", "Hello Bob!"])
        assert result.exit_code == 0
        assert "bob@test" in result.output
        mock_history.store_message.assert_called_once()

    @patch("skchat.cli._try_deliver", return_value={"delivered": False, "error": "no transport", "transport": None})
    @patch("skchat.cli._get_history")
    @patch("skchat.cli._get_identity", return_value="capauth:local@skchat")
    def test_send_with_thread(
        self,
        mock_id: MagicMock,
        mock_hist_fn: MagicMock,
        mock_deliver: MagicMock,
        mock_history: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Send with thread ID sets the thread on the message."""
        mock_hist_fn.return_value = mock_history

        result = runner.invoke(
            main, ["send", "capauth:bob@test", "Thread msg", "--thread", "abc123"]
        )
        assert result.exit_code == 0
        call_args = mock_history.store_message.call_args[0][0]
        assert call_args.thread_id == "abc123"

    @patch("skchat.cli._try_deliver", return_value={"delivered": False, "error": "no transport", "transport": None})
    @patch("skchat.cli._get_history")
    @patch("skchat.cli._get_identity", return_value="capauth:local@skchat")
    def test_send_ephemeral(
        self,
        mock_id: MagicMock,
        mock_hist_fn: MagicMock,
        mock_deliver: MagicMock,
        mock_history: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Send with TTL creates an ephemeral message."""
        mock_hist_fn.return_value = mock_history

        result = runner.invoke(
            main, ["send", "capauth:bob@test", "Secret", "--ttl", "60"]
        )
        assert result.exit_code == 0
        call_args = mock_history.store_message.call_args[0][0]
        assert call_args.ttl == 60

    def test_send_missing_args(self, runner: CliRunner) -> None:
        """Failure: missing required arguments shows error."""
        result = runner.invoke(main, ["send"])
        assert result.exit_code != 0


class TestInboxCommand:
    """Tests for the 'skchat inbox' command."""

    @patch("skchat.cli._get_history")
    @patch("skchat.cli._get_identity", return_value="capauth:local@skchat")
    def test_inbox_empty(
        self,
        mock_id: MagicMock,
        mock_hist_fn: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Edge case: empty inbox shows appropriate message."""
        history = MagicMock()
        history.search_messages.return_value = []
        history._store = MagicMock()
        history._store.list_memories.return_value = []
        history._memory_to_chat_dict = MagicMock()
        mock_hist_fn.return_value = history

        result = runner.invoke(main, ["inbox"])
        assert result.exit_code == 0
        assert "No messages" in result.output

    @patch("skchat.cli._get_history")
    @patch("skchat.cli._get_identity", return_value="capauth:local@skchat")
    def test_inbox_with_thread_filter(
        self,
        mock_id: MagicMock,
        mock_hist_fn: MagicMock,
        mock_history: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Inbox with --thread filters to that thread."""
        mock_hist_fn.return_value = mock_history

        result = runner.invoke(main, ["inbox", "--thread", "thread-abc"])
        assert result.exit_code == 0
        mock_history.get_thread_messages.assert_called_once_with("thread-abc", limit=20)

    @patch("skchat.cli._save_read_state")
    @patch("skchat.cli._load_read_state", return_value={})
    @patch("skchat.cli._get_history")
    @patch("skchat.cli._get_identity", return_value="capauth:local@skchat")
    def test_inbox_json_flag(
        self,
        mock_id: MagicMock,
        mock_hist_fn: MagicMock,
        mock_load: MagicMock,
        mock_save: MagicMock,
        mock_history: MagicMock,
        runner: CliRunner,
    ) -> None:
        """--json outputs a raw JSON array, no Rich markup."""
        history = MagicMock()
        msg = MagicMock()
        msg.sender = "capauth:alice@test"
        msg.recipient = "capauth:local@skchat"
        msg.content = "The quantum upgrade is ready"
        msg.thread_id = None
        msg.timestamp = "2026-02-23T14:00:00"
        history.load.return_value = [msg]
        mock_hist_fn.return_value = history

        result = runner.invoke(main, ["inbox", "--json"])
        assert result.exit_code == 0
        import json
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["content"] == "The quantum upgrade is ready"
        # early return → save_read_state NOT called
        mock_save.assert_not_called()

    @patch("skchat.cli._save_read_state")
    @patch("skchat.cli._load_read_state", return_value={})
    @patch("skchat.cli._get_history")
    @patch("skchat.cli._get_identity", return_value="capauth:local@skchat")
    def test_inbox_threads_flag(
        self,
        mock_id: MagicMock,
        mock_hist_fn: MagicMock,
        mock_load: MagicMock,
        mock_save: MagicMock,
        mock_history: MagicMock,
        runner: CliRunner,
    ) -> None:
        """--threads shows a one-line-per-conversation summary."""
        history = MagicMock()
        msg = MagicMock()
        msg.sender = "capauth:alice@test"
        msg.recipient = "capauth:local@skchat"
        msg.content = "Hey! The pipeline is live!"
        msg.thread_id = None
        msg.timestamp = "2026-02-23T14:00:00"
        history.load.return_value = [msg]
        mock_hist_fn.return_value = history

        result = runner.invoke(main, ["inbox", "--threads"])
        assert result.exit_code == 0
        # _display_name("capauth:alice@test") → "Alice"
        assert "Alice" in result.output
        mock_save.assert_called_once()

    @patch("skchat.cli._save_read_state")
    @patch("skchat.cli._load_read_state", return_value={})
    @patch("skchat.cli._get_history")
    @patch("skchat.cli._get_identity", return_value="capauth:local@skchat")
    def test_inbox_unread_flag_no_prior_state(
        self,
        mock_id: MagicMock,
        mock_hist_fn: MagicMock,
        mock_load: MagicMock,
        mock_save: MagicMock,
        mock_history: MagicMock,
        runner: CliRunner,
    ) -> None:
        """--unread with empty read-state shows all messages and saves state."""
        history = MagicMock()
        msg = MagicMock()
        msg.sender = "capauth:alice@test"
        msg.recipient = "capauth:local@skchat"
        msg.content = "The quantum upgrade is ready"
        msg.thread_id = None
        msg.timestamp = "2026-02-23T14:00:00"
        history.load.return_value = [msg]
        mock_hist_fn.return_value = history

        result = runner.invoke(main, ["inbox", "--unread"])
        assert result.exit_code == 0
        assert "quantum" in result.output.lower()
        mock_save.assert_called_once()

    @patch("skchat.cli._save_read_state")
    @patch(
        "skchat.cli._load_read_state",
        return_value={"_global": "2099-01-01T00:00:00"},
    )
    @patch("skchat.cli._get_history")
    @patch("skchat.cli._get_identity", return_value="capauth:local@skchat")
    def test_inbox_unread_flag_all_read(
        self,
        mock_id: MagicMock,
        mock_hist_fn: MagicMock,
        mock_load: MagicMock,
        mock_save: MagicMock,
        mock_history: MagicMock,
        runner: CliRunner,
    ) -> None:
        """--unread with a future last-read marker shows 'No unread messages'."""
        history = MagicMock()
        msg = MagicMock()
        msg.sender = "capauth:alice@test"
        msg.recipient = "capauth:local@skchat"
        msg.content = "Old news"
        msg.thread_id = None
        msg.timestamp = "2026-02-23T14:00:00"
        history.load.return_value = [msg]
        mock_hist_fn.return_value = history

        result = runner.invoke(main, ["inbox", "--unread"])
        assert result.exit_code == 0
        assert "No unread messages" in result.output
        mock_save.assert_not_called()


class TestInboxHelpers:
    """Unit tests for inbox display helper functions."""

    def test_display_name_capauth_uri(self) -> None:
        """capauth URI extracts local part and capitalises it."""
        from skchat.cli import _display_name
        assert _display_name("capauth:lumina@skworld.io") == "Lumina"
        assert _display_name("capauth:chef@skworld.io") == "Chef"
        assert _display_name("capauth:local@skchat") == "Local"

    def test_display_name_plain(self) -> None:
        """Plain strings are returned capitalised."""
        from skchat.cli import _display_name
        assert _display_name("alice") == "Alice"

    def test_sender_color_self(self) -> None:
        """Own identity returns blue."""
        from skchat.cli import _sender_color
        assert _sender_color("capauth:me@test", "capauth:me@test") == "blue"

    def test_sender_color_lumina(self) -> None:
        """Lumina gets magenta."""
        from skchat.cli import _sender_color
        assert _sender_color("capauth:lumina@skworld.io", "capauth:me@test") == "magenta"

    def test_sender_color_chef(self) -> None:
        """Chef gets yellow."""
        from skchat.cli import _sender_color
        assert _sender_color("capauth:chef@skworld.io", "capauth:me@test") == "yellow"

    def test_sender_color_other(self) -> None:
        """Unknown senders get cyan."""
        from skchat.cli import _sender_color
        assert _sender_color("capauth:bob@test", "capauth:me@test") == "cyan"

    def test_ts_ago_seconds(self) -> None:
        """Recent timestamp returns 'Xs ago'."""
        from skchat.cli import _ts_ago
        from datetime import timedelta
        ts = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        assert "s ago" in _ts_ago(ts)

    def test_ts_ago_minutes(self) -> None:
        """Minute-range timestamp returns 'Nmin ago'."""
        from skchat.cli import _ts_ago
        from datetime import timedelta
        ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        assert "min ago" in _ts_ago(ts)

    def test_ts_hhmm_string(self) -> None:
        """ISO string extracts HH:MM."""
        from skchat.cli import _ts_hhmm
        assert _ts_hhmm("2026-02-23T14:35:00") == "14:35"

    def test_ts_hhmm_short(self) -> None:
        """Short strings return as-is truncated."""
        from skchat.cli import _ts_hhmm
        assert _ts_hhmm("14:35") == "14:35"


class TestHistoryCommand:
    """Tests for the 'skchat history' command."""

    @patch("skchat.cli._get_history")
    @patch("skchat.cli._get_identity", return_value="capauth:local@skchat")
    def test_history_basic(
        self,
        mock_id: MagicMock,
        mock_hist_fn: MagicMock,
        mock_history: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Happy path: show conversation history."""
        mock_hist_fn.return_value = mock_history

        result = runner.invoke(main, ["history", "capauth:bob@test"])
        assert result.exit_code == 0
        assert "bob@test" in result.output

    @patch("skchat.cli._get_history")
    @patch("skchat.cli._get_identity", return_value="capauth:local@skchat")
    def test_history_empty(
        self,
        mock_id: MagicMock,
        mock_hist_fn: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Edge case: no history with a participant."""
        history = MagicMock()
        history.get_conversation.return_value = []
        mock_hist_fn.return_value = history

        result = runner.invoke(main, ["history", "capauth:nobody@test"])
        assert result.exit_code == 0
        assert "No conversation history" in result.output

    @patch("skchat.cli._get_history")
    @patch("skchat.cli._get_identity", return_value="capauth:local@skchat")
    def test_history_with_limit(
        self,
        mock_id: MagicMock,
        mock_hist_fn: MagicMock,
        mock_history: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Custom limit is passed through to get_conversation."""
        mock_hist_fn.return_value = mock_history

        result = runner.invoke(main, ["history", "capauth:bob@test", "--limit", "5"])
        assert result.exit_code == 0
        mock_history.get_conversation.assert_called_once_with(
            "capauth:local@skchat", "capauth:bob@test", limit=5
        )


class TestThreadsCommand:
    """Tests for the 'skchat threads' command."""

    @patch("skchat.cli._get_history")
    def test_threads_basic(
        self,
        mock_hist_fn: MagicMock,
        mock_history: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Happy path: list threads."""
        mock_hist_fn.return_value = mock_history

        result = runner.invoke(main, ["threads"])
        assert result.exit_code == 0
        assert "Dev Chat" in result.output

    @patch("skchat.cli._get_history")
    def test_threads_empty(
        self,
        mock_hist_fn: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Edge case: no threads."""
        history = MagicMock()
        history.list_threads.return_value = []
        mock_hist_fn.return_value = history

        result = runner.invoke(main, ["threads"])
        assert result.exit_code == 0
        assert "No threads" in result.output


class TestSearchCommand:
    """Tests for the 'skchat search' command."""

    @patch("skchat.cli._get_history")
    def test_search_with_results(
        self,
        mock_hist_fn: MagicMock,
        mock_history: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Happy path: search returns matching messages."""
        mock_hist_fn.return_value = mock_history

        result = runner.invoke(main, ["search", "quantum"])
        assert result.exit_code == 0
        assert "quantum" in result.output.lower()

    @patch("skchat.cli._get_history")
    def test_search_no_results(
        self,
        mock_hist_fn: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Edge case: search with no matches."""
        history = MagicMock()
        history.search_messages.return_value = []
        mock_hist_fn.return_value = history

        result = runner.invoke(main, ["search", "nonexistent"])
        assert result.exit_code == 0
        assert "No messages matching" in result.output

    def test_search_missing_query(self, runner: CliRunner) -> None:
        """Failure: search without query argument."""
        result = runner.invoke(main, ["search"])
        assert result.exit_code != 0


class TestReceiveCommand:
    """Tests for the 'skchat receive' command."""

    @patch("skchat.cli._get_chat_transport", return_value=None)
    def test_receive_no_transport(
        self,
        mock_transport: MagicMock,
        runner: CliRunner,
    ) -> None:
        """No transport shows configure message."""
        result = runner.invoke(main, ["receive"])
        assert result.exit_code == 0
        assert "No transports" in result.output

    @patch("skchat.cli._get_chat_transport")
    def test_receive_empty_inbox(
        self,
        mock_transport_fn: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Empty inbox shows no messages."""
        transport = MagicMock()
        transport.poll_inbox.return_value = []
        mock_transport_fn.return_value = transport

        result = runner.invoke(main, ["receive"])
        assert result.exit_code == 0
        assert "No new messages" in result.output

    @patch("skchat.cli._get_chat_transport")
    def test_receive_with_messages(
        self,
        mock_transport_fn: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Received messages are displayed."""
        from skchat.models import ChatMessage

        msg = ChatMessage(
            sender="capauth:bob@skworld.io",
            recipient="capauth:local@skchat",
            content="Hello from Bob!",
        )
        transport = MagicMock()
        transport.poll_inbox.return_value = [msg]
        mock_transport_fn.return_value = transport

        result = runner.invoke(main, ["receive"])
        assert result.exit_code == 0
        assert "bob@skworld.io" in result.output


class TestWatchCommand:
    """Tests for the 'skchat watch' command."""

    @patch("skchat.cli._get_transport", return_value=None)
    def test_watch_no_transport(
        self,
        mock_transport: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Watch with no transport shows configure message."""
        result = runner.invoke(main, ["watch"])
        assert result.exit_code == 0
        assert "No transport" in result.output


class TestBuildWatchTable:
    """Tests for the watch table builder."""

    def test_empty_table(self) -> None:
        """Empty messages shows waiting state."""
        from skchat.cli import _build_watch_table

        panel = _build_watch_table([], 0)
        assert panel is not None

    def test_table_with_messages(self) -> None:
        """Messages appear in the table."""
        from skchat.cli import _build_watch_table
        from skchat.models import ChatMessage

        msg = ChatMessage(
            sender="capauth:alice@skworld.io",
            recipient="capauth:bob@skworld.io",
            content="Test message",
        )
        panel = _build_watch_table([msg], 1)
        assert panel is not None


class TestStatusCommand:
    """Tests for the 'skchat status' command."""

    @patch("skchat.cli._get_history")
    @patch("skchat.cli._get_identity", return_value="capauth:local@skchat")
    def test_status(
        self,
        mock_id: MagicMock,
        mock_hist_fn: MagicMock,
        mock_history: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Happy path: status shows identity and counts."""
        mock_hist_fn.return_value = mock_history

        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        assert "capauth:local@skchat" in result.output
        assert "42" in result.output
