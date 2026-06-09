"""Tests for chat daemon — background message receiving."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skchat.daemon import (
    ChatDaemon,
    DaemonShutdown,
    _acquire_singleton_lock,
    _live_daemon_pids,
    _read_pid,
    _singleton_lock_held,
    _remove_pid,
    _write_pid,
    daemon_status,
    is_running,
    run_daemon,
    start_daemon,
    stop_daemon,
)
from skchat.models import ChatMessage, DeliveryStatus


@pytest.fixture
def mock_transport():
    """Create a mock ChatTransport."""
    transport = MagicMock()
    transport.poll_inbox = MagicMock(return_value=[])
    return transport


@pytest.fixture
def mock_history():
    """Create a mock ChatHistory."""
    history = MagicMock()
    history.store_message = MagicMock()
    return history


@pytest.fixture
def sample_message():
    """Create a sample ChatMessage for testing."""
    return ChatMessage(
        sender="capauth:alice@capauth.local",
        recipient="capauth:bob@capauth.local",
        content="Test message",
        delivery_status=DeliveryStatus.DELIVERED,
    )


def test_daemon_init():
    """Test daemon initialization."""
    daemon = ChatDaemon(interval=10, quiet=True)
    assert daemon.interval == 10
    assert daemon.quiet is True
    assert daemon.running is False
    assert daemon.total_received == 0


def test_daemon_init_with_log_file(tmp_path):
    """Test daemon initialization with log file."""
    log_file = tmp_path / "daemon.log"
    daemon = ChatDaemon(interval=5, log_file=log_file, quiet=False)
    assert daemon.log_file == log_file
    assert daemon.quiet is False


def test_daemon_uptime():
    """Test uptime calculation."""
    daemon = ChatDaemon(interval=5, quiet=True)

    daemon.poll_count = 0
    daemon.last_poll_time = None
    assert daemon._uptime() == "0s"

    from datetime import datetime, timezone

    daemon.last_poll_time = datetime.now(timezone.utc)
    daemon.poll_count = 10
    assert daemon._uptime() == "50s"

    daemon.poll_count = 120
    assert daemon._uptime() == "10m 0s"

    daemon.poll_count = 1440
    assert daemon._uptime() == "2h 0m"


def test_daemon_log_quiet(capsys):
    """Test logging with quiet mode."""
    daemon = ChatDaemon(interval=5, quiet=True)
    daemon._log("Test message")

    captured = capsys.readouterr()
    assert captured.out == ""


def test_daemon_log_verbose(capsys):
    """Test logging with verbose mode."""
    daemon = ChatDaemon(interval=5, quiet=False)
    daemon._log("Test message")

    captured = capsys.readouterr()
    assert "Test message" in captured.out


@patch("skchat.daemon.time.sleep", return_value=None)
@patch("skchat.daemon.SKComm")
@patch("skchat.transport.ChatTransport")
@patch("skchat.history.ChatHistory")
@patch("skchat.identity_bridge.get_sovereign_identity")
def test_daemon_start_no_messages(
    mock_identity,
    mock_history_class,
    mock_transport_class,
    mock_skcomm_class,
    mock_sleep,
    mock_transport,
):
    """Test daemon with no incoming messages."""
    mock_skcomm_class.from_config.return_value = mock_skcomm_class
    mock_transport_class.from_config.return_value = mock_transport
    mock_identity.return_value = "capauth:test@capauth.local"

    daemon = ChatDaemon(interval=0.1, quiet=True)

    # Stop after 3 poll cycles via sleep call count (instant sleeps, count-based)
    call_count = [0]

    def _counting_sleep(seconds):
        call_count[0] += 1
        if call_count[0] >= 3:
            daemon.running = False

    mock_sleep.side_effect = _counting_sleep

    daemon.start()

    assert daemon.poll_count >= 2
    assert daemon.total_received == 0


@patch("skchat.daemon.time.sleep", return_value=None)
@patch("skchat.daemon.SKComm")
@patch("skchat.transport.ChatTransport")
@patch("skchat.history.ChatHistory")
@patch("skchat.identity_bridge.get_sovereign_identity")
def test_daemon_start_with_messages(
    mock_identity,
    mock_history_class,
    mock_transport_class,
    mock_skcomm_class,
    mock_sleep,
    mock_transport,
    sample_message,
):
    """Test daemon receiving messages."""
    mock_skcomm_class.from_config.return_value = mock_skcomm_class
    mock_transport.poll_inbox = MagicMock(return_value=[sample_message])
    mock_transport_class.from_config.return_value = mock_transport
    mock_identity.return_value = "capauth:test@capauth.local"

    daemon = ChatDaemon(interval=0.1, quiet=True)

    call_count = [0]

    def _counting_sleep(seconds):
        call_count[0] += 1
        if call_count[0] >= 3:
            daemon.running = False

    mock_sleep.side_effect = _counting_sleep

    daemon.start()

    assert daemon.total_received >= 2


@patch("skchat.transport.ChatTransport")
@patch("skchat.history.ChatHistory")
@patch("skchat.identity_bridge.get_sovereign_identity")
def test_daemon_graceful_shutdown(
    mock_identity,
    mock_history_class,
    mock_transport_class,
    mock_transport,
):
    """Test daemon graceful shutdown on signal."""
    mock_transport_class.from_config.return_value = mock_transport
    mock_identity.return_value = "capauth:test@capauth.local"

    daemon = ChatDaemon(interval=0.1, quiet=True)

    with pytest.raises(DaemonShutdown):
        daemon._handle_signal(15, None)

    assert daemon.running is False


@patch("skchat.daemon.time.sleep", return_value=None)
@patch("skchat.daemon.SKComm")
@patch("skchat.transport.ChatTransport")
@patch("skchat.history.ChatHistory")
@patch("skchat.identity_bridge.get_sovereign_identity")
def test_daemon_poll_error_handling(
    mock_identity,
    mock_history_class,
    mock_transport_class,
    mock_skcomm_class,
    mock_sleep,
    mock_transport,
):
    """Test daemon handling poll errors gracefully — backoff sleep is bypassed."""
    mock_skcomm_class.from_config.return_value = mock_skcomm_class
    mock_transport.poll_inbox = MagicMock(side_effect=Exception("Transport error"))
    mock_transport_class.from_config.return_value = mock_transport
    mock_identity.return_value = "capauth:test@capauth.local"

    daemon = ChatDaemon(interval=0.1, quiet=True)

    call_count = [0]

    def _counting_sleep(seconds):
        call_count[0] += 1
        if call_count[0] >= 3:
            daemon.running = False

    mock_sleep.side_effect = _counting_sleep

    daemon.start()

    # Daemon should have attempted multiple polls (errors don't stop the loop)
    assert daemon.poll_count >= 2
    assert daemon._consecutive_failures >= 2


@patch("skchat.daemon.SKComm")
@patch("skchat.history.ChatHistory")
@patch("skchat.transport.ChatTransport")
@patch("skchat.identity_bridge.get_sovereign_identity")
def test_daemon_start_transport_init_failure(
    mock_identity,
    mock_transport_class,
    mock_history_class,
    mock_skcomm_class,
):
    """Test daemon handling transport initialization failure."""
    mock_skcomm_class.from_config.return_value = mock_skcomm_class
    mock_history_class.from_config.return_value = MagicMock()
    mock_identity.return_value = "capauth:test@capauth.local"
    mock_transport_class.from_config.side_effect = Exception("No transport")

    daemon = ChatDaemon(interval=5, quiet=True)

    with pytest.raises(Exception, match="No transport"):
        daemon.start()


def test_daemon_from_config_defaults():
    """Test creating daemon from config with defaults."""
    daemon = ChatDaemon.from_config()
    assert daemon.interval == 5.0
    assert daemon.log_file is None
    assert daemon.quiet is False


def test_daemon_from_config_env_vars():
    """Test creating daemon from environment variables."""
    with patch.dict(
        "os.environ",
        {
            "SKCHAT_DAEMON_INTERVAL": "10.0",
            "SKCHAT_DAEMON_LOG": "/tmp/daemon.log",
            "SKCHAT_DAEMON_QUIET": "true",
        },
    ):
        daemon = ChatDaemon.from_config()
        assert daemon.interval == 10.0
        assert daemon.log_file == Path("/tmp/daemon.log")
        assert daemon.quiet is True


def test_daemon_from_config_yaml(tmp_path):
    """Test creating daemon from YAML config file."""
    pytest.importorskip("yaml")

    config_file = tmp_path / "config.yml"
    config_content = """
daemon:
  poll_interval: 15
  log_file: /var/log/skchat.log
  quiet: true
"""
    with open(config_file, "w") as f:
        f.write(config_content)

    daemon = ChatDaemon.from_config(config_file)
    assert daemon.interval == 15
    assert daemon.log_file == Path("/var/log/skchat.log")
    assert daemon.quiet is True


@patch("skchat.daemon._acquire_singleton_lock", return_value=True)
@patch("skchat.daemon.ChatDaemon")
def test_run_daemon(mock_daemon_class, _mock_lock):
    """Test run_daemon wrapper function."""
    mock_daemon = MagicMock()
    mock_daemon_class.return_value = mock_daemon

    run_daemon(interval=10, log_file="/tmp/test.log", quiet=True)

    mock_daemon_class.assert_called_once()
    mock_daemon.start.assert_called_once()


@patch("skchat.daemon._acquire_singleton_lock", return_value=True)
@patch("skchat.daemon.ChatDaemon")
def test_run_daemon_with_exception(mock_daemon_class, _mock_lock):
    """Test run_daemon handling exceptions."""
    mock_daemon = MagicMock()
    mock_daemon.start.side_effect = Exception("Daemon error")
    mock_daemon_class.return_value = mock_daemon

    with pytest.raises(SystemExit):
        run_daemon()


@patch("skchat.daemon.ChatDaemon")
def test_run_daemon_refuses_duplicate(mock_daemon_class, monkeypatch):
    """run_daemon exits 0 (not a failure) when another daemon holds the lock."""
    monkeypatch.setattr("skchat.daemon._acquire_singleton_lock", lambda: False)
    monkeypatch.setattr("skchat.daemon._live_daemon_pids", lambda: [4242])
    with pytest.raises(SystemExit) as exc:
        run_daemon()
    assert exc.value.code == 0
    mock_daemon_class.assert_not_called()  # never even constructed the daemon


# ---------------------------------------------------------------------------
# PID file management tests
# ---------------------------------------------------------------------------


class TestPidFile:
    """Tests for PID file read/write/remove helpers."""

    def test_write_and_read_pid(self, tmp_path, monkeypatch):
        """Expected: write PID then read it back returns the same int."""
        import skchat.daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "DAEMON_PID_FILE", tmp_path / "daemon.pid")
        _write_pid(12345)
        assert _read_pid() == 12345

    def test_read_pid_missing_file(self, tmp_path, monkeypatch):
        """Expected: read_pid returns None when PID file is absent."""
        import skchat.daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "DAEMON_PID_FILE", tmp_path / "daemon.pid")
        assert _read_pid() is None

    def test_remove_pid(self, tmp_path, monkeypatch):
        """Expected: remove_pid deletes the file."""
        import skchat.daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "DAEMON_PID_FILE", tmp_path / "daemon.pid")
        _write_pid(999)
        _remove_pid()
        assert not (tmp_path / "daemon.pid").exists()

    def test_remove_pid_idempotent(self, tmp_path, monkeypatch):
        """Edge case: remove_pid does not raise if file is absent."""
        import skchat.daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "DAEMON_PID_FILE", tmp_path / "daemon.pid")
        _remove_pid()  # no error


class TestIsRunning:
    """Tests for the is_running() helper."""

    def test_not_running_no_pid_file(self, tmp_path, monkeypatch):
        """Expected: not running when PID file is absent."""
        import skchat.daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "DAEMON_PID_FILE", tmp_path / "daemon.pid")
        assert is_running() is False

    def test_not_running_stale_pid(self, tmp_path, monkeypatch):
        """Edge case: stale PID (process not found) returns False.

        Note: is_running() is intentionally PID-file-only (not process-scan
        aware) so it never race-blocks a systemd restart; the flock is what
        actually prevents a duplicate in this desync case.
        """
        import skchat.daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "DAEMON_PID_FILE", tmp_path / "daemon.pid")
        _write_pid(999999999)  # very unlikely to exist
        assert is_running() is False

    def test_running_own_process(self, tmp_path, monkeypatch):
        """Expected: process is running when PID is the current process."""
        import os

        import skchat.daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "DAEMON_PID_FILE", tmp_path / "daemon.pid")
        _write_pid(os.getpid())
        assert is_running() is True
        _remove_pid()


class TestDaemonStatus:
    """Tests for daemon_status()."""

    def test_status_stopped(self, tmp_path, monkeypatch):
        """Expected: status is stopped when no PID file and no daemon process."""
        import skchat.daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "DAEMON_PID_FILE", tmp_path / "daemon.pid")
        monkeypatch.setattr(daemon_mod, "DAEMON_LOG_FILE", tmp_path / "daemon.log")
        info = daemon_status()
        assert info["running"] is False
        assert info["pid"] is None

    def test_status_running(self, tmp_path, monkeypatch):
        """Expected: status is running when current PID is stored."""
        import os

        import skchat.daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "DAEMON_PID_FILE", tmp_path / "daemon.pid")
        monkeypatch.setattr(daemon_mod, "DAEMON_LOG_FILE", tmp_path / "daemon.log")
        _write_pid(os.getpid())
        info = daemon_status()
        assert info["running"] is True
        assert info["pid"] == os.getpid()
        _remove_pid()

    def test_status_stale_pid_cleaned(self, tmp_path, monkeypatch):
        """Edge case: stale PID is cleaned up and reported as stopped."""
        import skchat.daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "DAEMON_PID_FILE", tmp_path / "daemon.pid")
        monkeypatch.setattr(daemon_mod, "DAEMON_LOG_FILE", tmp_path / "daemon.log")
        _write_pid(999999999)
        info = daemon_status()
        assert info["running"] is False
        assert info["pid"] is None
        assert not (tmp_path / "daemon.pid").exists()


class TestStartStopDaemon:
    """Tests for start_daemon() and stop_daemon()."""

    def test_start_daemon_already_running_raises(self, tmp_path, monkeypatch):
        """Failure case: starting when already running raises RuntimeError."""
        import os

        import skchat.daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "DAEMON_PID_FILE", tmp_path / "daemon.pid")
        _write_pid(os.getpid())
        with pytest.raises(RuntimeError, match="already running"):
            start_daemon(background=True)
        _remove_pid()

    def test_stop_daemon_not_running(self, tmp_path, monkeypatch):
        """Expected: stop when not running returns None gracefully."""
        import skchat.daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "DAEMON_PID_FILE", tmp_path / "daemon.pid")
        result = stop_daemon()
        assert result is None


# ---------------------------------------------------------------------------
# CLI daemon subcommand tests
# ---------------------------------------------------------------------------


class TestDaemonCLI:
    """Tests for skchat daemon start/stop/status CLI subcommands."""

    def test_daemon_status_stopped(self, tmp_path, monkeypatch):
        """Expected: daemon status shows stopped when not running."""
        from click.testing import CliRunner

        import skchat.daemon as daemon_mod
        from skchat.cli import main

        monkeypatch.setattr(daemon_mod, "DAEMON_PID_FILE", tmp_path / "daemon.pid")
        monkeypatch.setattr(daemon_mod, "DAEMON_LOG_FILE", tmp_path / "daemon.log")

        runner = CliRunner()
        result = runner.invoke(main, ["daemon", "status"])
        assert result.exit_code == 0
        assert "stopped" in result.output.lower()

    def test_daemon_status_running(self, tmp_path, monkeypatch):
        """Expected: daemon status shows running when PID is current process."""
        import os

        from click.testing import CliRunner

        import skchat.daemon as daemon_mod
        from skchat.cli import main

        monkeypatch.setattr(daemon_mod, "DAEMON_PID_FILE", tmp_path / "daemon.pid")
        monkeypatch.setattr(daemon_mod, "DAEMON_LOG_FILE", tmp_path / "daemon.log")
        _write_pid(os.getpid())

        runner = CliRunner()
        result = runner.invoke(main, ["daemon", "status"])
        assert result.exit_code == 0
        assert "running" in result.output.lower()
        _remove_pid()

    def test_daemon_stop_when_not_running(self, tmp_path, monkeypatch):
        """Expected: daemon stop shows no daemon running message."""
        from click.testing import CliRunner

        import skchat.daemon as daemon_mod
        from skchat.cli import main

        monkeypatch.setattr(daemon_mod, "DAEMON_PID_FILE", tmp_path / "daemon.pid")

        runner = CliRunner()
        result = runner.invoke(main, ["daemon", "stop"])
        assert result.exit_code == 0
        assert "no daemon" in result.output.lower()

    def test_daemon_help(self):
        """Expected: daemon help shows subcommands."""
        from click.testing import CliRunner

        from skchat.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["daemon", "--help"])
        assert result.exit_code == 0
        assert "start" in result.output
        assert "stop" in result.output
        assert "status" in result.output


# ---------------------------------------------------------------------------
# Single-instance enforcement (duplicate-daemon prevention)
# ---------------------------------------------------------------------------


class TestLiveDaemonPids:
    """Tests for the process-table scan that backs single-instance enforcement."""

    def test_excludes_self_and_returns_list(self):
        """The scan never reports the calling process and returns a list."""
        import os

        pids = _live_daemon_pids()
        assert isinstance(pids, list)
        assert os.getpid() not in pids

    def test_detects_marker_process(self):
        """A real process whose cmdline carries the daemon marker is found."""
        import subprocess
        import sys
        import time

        # The -c source text contains the marker, so it appears in /proc cmdline.
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(5)  # skchat._daemon_entry"]
        )
        try:
            found = None
            for _ in range(20):  # poll up to ~2s for the process to appear
                if proc.pid in _live_daemon_pids():
                    found = True
                    break
                time.sleep(0.1)
            assert found, "marker process not detected by _live_daemon_pids()"
        finally:
            proc.terminate()
            proc.wait(timeout=5)


class TestSingletonLock:
    """Tests for the flock-based single-instance lock and start guard."""

    def test_second_acquire_is_denied(self, tmp_path, monkeypatch):
        """A second flock attempt on the same lock file is refused (race-free)."""
        import skchat.daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "DAEMON_LOCK_FILE", tmp_path / "daemon.lock")
        monkeypatch.setattr(daemon_mod, "_daemon_lock_handle", None)
        try:
            # retries=0 → no retry delay; the second holder is denied immediately.
            assert _acquire_singleton_lock(retries=0) is True   # first holder wins
            assert _acquire_singleton_lock(retries=0) is False  # second is denied
        finally:
            # Release so the lock file handle doesn't leak into other tests.
            if daemon_mod._daemon_lock_handle is not None:
                daemon_mod._daemon_lock_handle.close()
                daemon_mod._daemon_lock_handle = None

    def test_foreground_start_refuses_when_lock_held(self, tmp_path, monkeypatch):
        """start_daemon(foreground) refuses when the singleton lock is already held —
        the desync-proof guard (no reliance on the PID file)."""
        import skchat.daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "DAEMON_PID_FILE", tmp_path / "daemon.pid")
        monkeypatch.setattr(daemon_mod, "_acquire_singleton_lock", lambda *a, **k: False)
        monkeypatch.setattr(daemon_mod, "_live_daemon_pids", lambda: [4242])
        # No PID file → is_running() is False, so only the flock stands between us
        # and a duplicate. It must still refuse.
        with pytest.raises(RuntimeError, match="already running"):
            start_daemon(background=False)

    def test_start_daemon_blocked_by_pidfile(self, tmp_path, monkeypatch):
        """Fast path: an in-sync live PID file short-circuits before forking."""
        import os

        import skchat.daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "DAEMON_PID_FILE", tmp_path / "daemon.pid")
        _write_pid(os.getpid())  # a live PID → is_running() True
        with pytest.raises(RuntimeError, match="already running"):
            start_daemon(background=True)
        _remove_pid()

    def test_lock_held_probe(self, tmp_path, monkeypatch):
        """The non-destructive probe reports free vs held without keeping a lock."""
        import skchat.daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "DAEMON_LOCK_FILE", tmp_path / "daemon.lock")
        monkeypatch.setattr(daemon_mod, "_daemon_lock_handle", None)
        assert _singleton_lock_held() is False  # nobody holds it yet
        try:
            assert _acquire_singleton_lock(retries=0) is True  # now we hold it
            assert _singleton_lock_held() is True              # probe sees the holder
        finally:
            if daemon_mod._daemon_lock_handle is not None:
                daemon_mod._daemon_lock_handle.close()
                daemon_mod._daemon_lock_handle = None

    def test_background_start_refused_by_lock_probe_when_pidfile_stale(self, tmp_path, monkeypatch):
        """The desync fix: a clobbered PID file does NOT let a duplicate fork —
        the lock probe refuses in the parent before any child is spawned."""
        import skchat.daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "DAEMON_PID_FILE", tmp_path / "daemon.pid")
        _write_pid(999999999)  # stale → is_running() False
        monkeypatch.setattr(daemon_mod, "_singleton_lock_held", lambda: True)
        monkeypatch.setattr(daemon_mod, "_live_daemon_pids", lambda: [4242])
        with pytest.raises(RuntimeError, match="4242"):
            start_daemon(background=True)
