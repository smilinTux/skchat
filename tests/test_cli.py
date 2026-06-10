"""Tests for SKChat CLI — Click commands via CliRunner."""

from __future__ import annotations

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
        """Happy path: --version prints the version string.

        Reads the canonical __version__ off the package so the test stays
        in sync with pyproject.toml (was previously pinned to 0.1.2).
        """
        from skchat import __version__

        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "skchat" in result.output
        assert __version__ in result.output

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

    @patch(
        "skchat.cli._try_deliver",
        return_value={"delivered": False, "error": "no transport", "transport": None},
    )
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

    @patch(
        "skchat.cli._try_deliver",
        return_value={"delivered": False, "error": "no transport", "transport": None},
    )
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

    @patch(
        "skchat.cli._try_deliver",
        return_value={"delivered": False, "error": "no transport", "transport": None},
    )
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

        result = runner.invoke(main, ["send", "capauth:bob@test", "Secret", "--ttl", "60"])
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
        from datetime import timedelta

        from skchat.cli import _ts_ago

        ts = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        assert "s ago" in _ts_ago(ts)

    def test_ts_ago_minutes(self) -> None:
        """Minute-range timestamp returns 'Nmin ago'."""
        from datetime import timedelta

        from skchat.cli import _ts_ago

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


class TestStatsCommand:
    """Tests for the 'skchat stats' command."""

    @staticmethod
    def _seed_history(history_dir, rows) -> None:
        """Write ChatMessage rows into a real ChatHistory JSONL store.

        Args:
            history_dir: tmp directory backing the ChatHistory.
            rows: list of (sender, recipient, content, thread_id, iso_ts).
        """
        from skchat.history import ChatHistory
        from skchat.models import ChatMessage

        hist = ChatHistory(store=MagicMock(), history_dir=history_dir)
        for sender, recipient, content, thread_id, iso_ts in rows:
            msg = ChatMessage(
                sender=sender,
                recipient=recipient,
                content=content,
                thread_id=thread_id,
                timestamp=datetime.fromisoformat(iso_ts),
            )
            # Write directly so the on-disk file name doesn't depend on "today".
            path = history_dir / f"{msg.timestamp.strftime('%Y-%m-%d')}.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(msg.model_dump_json())
                fh.write("\n")

    @patch("skchat.cli._get_history")
    def test_stats_counts(
        self,
        mock_hist_fn: MagicMock,
        runner: CliRunner,
        tmp_path,
    ) -> None:
        """Happy path: totals, by-sender, by-day are all correct."""
        from skchat.history import ChatHistory

        hist_dir = tmp_path / "history"
        hist_dir.mkdir()
        self._seed_history(
            hist_dir,
            [
                ("capauth:alice@test", "capauth:bob@test", "hi", "t1", "2026-02-23T10:00:00+00:00"),
                ("capauth:alice@test", "capauth:bob@test", "yo", "t1", "2026-02-23T11:00:00+00:00"),
                ("capauth:bob@test", "capauth:alice@test", "sup", None, "2026-02-24T09:00:00+00:00"),
            ],
        )
        mock_hist_fn.return_value = ChatHistory(store=MagicMock(), history_dir=hist_dir)

        result = runner.invoke(main, ["stats"])
        assert result.exit_code == 0
        # total = 3
        assert "3" in result.output
        # both senders represented
        assert "alice" in result.output.lower()
        assert "bob" in result.output.lower()

    @patch("skchat.cli._get_history")
    def test_stats_empty(
        self,
        mock_hist_fn: MagicMock,
        runner: CliRunner,
        tmp_path,
    ) -> None:
        """Edge case: empty history is handled gracefully."""
        from skchat.history import ChatHistory

        hist_dir = tmp_path / "history"
        hist_dir.mkdir()
        mock_hist_fn.return_value = ChatHistory(store=MagicMock(), history_dir=hist_dir)

        result = runner.invoke(main, ["stats"])
        assert result.exit_code == 0
        assert "No messages" in result.output

    @patch("skchat.cli._get_history")
    def test_stats_json_out(
        self,
        mock_hist_fn: MagicMock,
        runner: CliRunner,
        tmp_path,
    ) -> None:
        """--json-out emits a well-shaped JSON document."""
        import json

        from skchat.history import ChatHistory

        hist_dir = tmp_path / "history"
        hist_dir.mkdir()
        self._seed_history(
            hist_dir,
            [
                ("capauth:alice@test", "capauth:bob@test", "hi", "t1", "2026-02-23T10:00:00+00:00"),
                ("capauth:bob@test", "capauth:alice@test", "yo", "t1", "2026-02-23T11:00:00+00:00"),
                ("capauth:bob@test", "capauth:alice@test", "x", None, "2026-02-24T09:00:00+00:00"),
            ],
        )
        mock_hist_fn.return_value = ChatHistory(store=MagicMock(), history_dir=hist_dir)

        result = runner.invoke(main, ["stats", "--json-out"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["total"] == 3
        assert data["by_sender"]["capauth:bob@test"] == 2
        assert data["by_sender"]["capauth:alice@test"] == 1
        assert data["by_day"]["2026-02-23"] == 2
        assert data["by_day"]["2026-02-24"] == 1
        assert data["first_timestamp"] == "2026-02-23T10:00:00+00:00"
        assert data["last_timestamp"] == "2026-02-24T09:00:00+00:00"
        # by_group keyed by thread/conversation grouping
        assert data["by_group"]["t1"] == 2


_METRICS_FIXTURE = """\
# HELP bridge_messages_processed_total Total messages processed by the bridge
# TYPE bridge_messages_processed_total counter
bridge_messages_processed_total 17

# HELP bridge_errors_total Total errors encountered by the bridge
# TYPE bridge_errors_total counter
bridge_errors_total 2

# HELP bridge_uptime_seconds Seconds since the bridge started
# TYPE bridge_uptime_seconds gauge
bridge_uptime_seconds 3600

# HELP bridge_last_response_timestamp Unix timestamp of the last sent response
# TYPE bridge_last_response_timestamp gauge
bridge_last_response_timestamp 1700000000
"""


class TestBridgeStatusHelpers:
    """Unit tests for the bridge-status parsing + collection helpers."""

    def test_parse_bridge_metrics(self) -> None:
        """Prometheus text is parsed into the named metric fields."""
        from skchat.cli import _parse_bridge_metrics

        parsed = _parse_bridge_metrics(_METRICS_FIXTURE)
        assert parsed["messages"] == 17
        assert parsed["errors"] == 2
        assert parsed["uptime_s"] == 3600

    def test_collect_both_up(self) -> None:
        """Both bridges reachable → both report up with their metrics."""
        from skchat.cli import _collect_bridge_statuses

        def fake_fetch(url: str) -> str:
            return _METRICS_FIXTURE

        rows = _collect_bridge_statuses(
            [("lumina", "http://x:9386/metrics"), ("opus", "http://x:9387/metrics")],
            fetch=fake_fetch,
        )
        assert len(rows) == 2
        assert all(r["up"] for r in rows)
        assert rows[0]["messages"] == 17
        assert rows[0]["errors"] == 2

    def test_collect_one_down(self) -> None:
        """A bridge whose fetch raises is reported down, not a crash."""
        from skchat.cli import _collect_bridge_statuses

        def fake_fetch(url: str) -> str:
            if "9387" in url:
                raise OSError("connection refused")
            return _METRICS_FIXTURE

        rows = _collect_bridge_statuses(
            [("lumina", "http://x:9386/metrics"), ("opus", "http://x:9387/metrics")],
            fetch=fake_fetch,
        )
        by_name = {r["name"]: r for r in rows}
        assert by_name["lumina"]["up"] is True
        assert by_name["opus"]["up"] is False

    def test_collect_all_down(self) -> None:
        """All bridges unreachable → all down, no exception."""
        from skchat.cli import _collect_bridge_statuses

        def fake_fetch(url: str) -> str:
            raise OSError("down")

        rows = _collect_bridge_statuses(
            [("lumina", "http://x:9386/metrics"), ("opus", "http://x:9387/metrics")],
            fetch=fake_fetch,
        )
        assert all(r["up"] is False for r in rows)


class TestBridgeStatusCommand:
    """Tests for the 'skchat bridge-status' command."""

    @patch("skchat.cli._default_bridge_fetch", return_value=_METRICS_FIXTURE)
    def test_bridge_status_both_up(
        self,
        mock_fetch: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Both bridges up → table shows up state and message counts."""
        result = runner.invoke(main, ["bridge-status"])
        assert result.exit_code == 0
        assert "lumina" in result.output
        assert "opus" in result.output
        assert "17" in result.output

    @patch("skchat.cli._default_bridge_fetch")
    def test_bridge_status_one_down(
        self,
        mock_fetch: MagicMock,
        runner: CliRunner,
    ) -> None:
        """One bridge down is rendered as down, command still exits 0."""

        def side(url: str) -> str:
            if "9387" in url:
                raise OSError("refused")
            return _METRICS_FIXTURE

        mock_fetch.side_effect = side
        result = runner.invoke(main, ["bridge-status"])
        assert result.exit_code == 0
        assert "down" in result.output.lower()

    @patch("skchat.cli._default_bridge_fetch", side_effect=OSError("down"))
    def test_bridge_status_json_out(
        self,
        mock_fetch: MagicMock,
        runner: CliRunner,
    ) -> None:
        """--json-out emits a JSON list; all-down reports up=false."""
        import json

        result = runner.invoke(main, ["bridge-status", "--json-out"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert all(row["up"] is False for row in data)


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
