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
    _remove_pid,
    _singleton_lock_held,
    _write_pid,
    daemon_status,
    is_running,
    run_daemon,
    start_daemon,
    stop_daemon,
    turn_secret_present,
    webrtc_signaling_health,
    webrtc_turn_warning,
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
@patch("skchat.daemon.SKComms")
@patch("skchat.transport.ChatTransport")
@patch("skchat.history.ChatHistory")
@patch("skchat.identity_bridge.get_sovereign_identity")
def test_daemon_start_no_messages(
    mock_identity,
    mock_history_class,
    mock_transport_class,
    mock_skcomms_class,
    mock_sleep,
    mock_transport,
):
    """Test daemon with no incoming messages."""
    mock_skcomms_class.from_config.return_value = mock_skcomms_class
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
@patch("skchat.daemon.SKComms")
@patch("skchat.transport.ChatTransport")
@patch("skchat.history.ChatHistory")
@patch("skchat.identity_bridge.get_sovereign_identity")
def test_daemon_start_with_messages(
    mock_identity,
    mock_history_class,
    mock_transport_class,
    mock_skcomms_class,
    mock_sleep,
    mock_transport,
    sample_message,
):
    """Test daemon receiving messages."""
    mock_skcomms_class.from_config.return_value = mock_skcomms_class
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
@patch("skchat.daemon.SKComms")
@patch("skchat.transport.ChatTransport")
@patch("skchat.history.ChatHistory")
@patch("skchat.identity_bridge.get_sovereign_identity")
def test_daemon_poll_error_handling(
    mock_identity,
    mock_history_class,
    mock_transport_class,
    mock_skcomms_class,
    mock_sleep,
    mock_transport,
):
    """Test daemon handling poll errors gracefully — backoff sleep is bypassed."""
    mock_skcomms_class.from_config.return_value = mock_skcomms_class
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


@patch("skchat.daemon.time.sleep", return_value=None)
@patch("skchat.daemon.SKComms")
@patch("skchat.transport.ChatTransport")
@patch("skchat.history.ChatHistory")
@patch("skchat.identity_bridge.get_sovereign_identity")
def test_daemon_poll_backoff_escalates_past_interval(
    mock_identity,
    mock_history_class,
    mock_transport_class,
    mock_skcomms_class,
    mock_sleep,
    mock_transport,
):
    """Regression for bughunt defect #1: consecutive transport-poll failures
    must sleep the escalating _BACKOFF_DELAYS (5/10/20/40/60), not get capped
    at `self.interval`. A prior `min(delay, self.interval)` made every sleep
    after the first equal to `self.interval` (5s in production) since
    interval <= every backoff tier past the first — the escalation was dead
    code. Use a small interval (0.1s) so a bug reintroducing the cap is
    caught regardless of interval size.
    """
    mock_skcomms_class.from_config.return_value = mock_skcomms_class
    mock_transport.poll_inbox = MagicMock(side_effect=Exception("Transport error"))
    mock_transport_class.from_config.return_value = mock_transport
    mock_identity.return_value = "capauth:test@capauth.local"

    daemon = ChatDaemon(interval=0.1, quiet=True)

    sleeps: list[float] = []

    def _recording_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 5:
            daemon.running = False

    mock_sleep.side_effect = _recording_sleep

    daemon.start()

    assert len(sleeps) >= 5
    # Escalating, uncapped by self.interval (0.1s) — proves the fix.
    assert sleeps[:5] == [5, 10, 20, 40, 60]


@patch("skchat.daemon.SKComms")
@patch("skchat.history.ChatHistory")
@patch("skchat.transport.ChatTransport")
@patch("skchat.identity_bridge.get_sovereign_identity")
def test_daemon_start_transport_init_failure(
    mock_identity,
    mock_transport_class,
    mock_history_class,
    mock_skcomms_class,
):
    """Test daemon handling transport initialization failure."""
    mock_skcomms_class.from_config.return_value = mock_skcomms_class
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
            assert _acquire_singleton_lock(retries=0) is True  # first holder wins
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
            assert _singleton_lock_held() is True  # probe sees the holder
        finally:
            if daemon_mod._daemon_lock_handle is not None:
                daemon_mod._daemon_lock_handle.close()
                daemon_mod._daemon_lock_handle = None

    def test_background_start_refused_by_lock_probe_when_pidfile_stale(
        self, tmp_path, monkeypatch
    ):
        """The desync fix: a clobbered PID file does NOT let a duplicate fork —
        the lock probe refuses in the parent before any child is spawned."""
        import skchat.daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "DAEMON_PID_FILE", tmp_path / "daemon.pid")
        _write_pid(999999999)  # stale → is_running() False
        monkeypatch.setattr(daemon_mod, "_singleton_lock_held", lambda: True)
        monkeypatch.setattr(daemon_mod, "_live_daemon_pids", lambda: [4242])
        with pytest.raises(RuntimeError, match="4242"):
            start_daemon(background=True)


# ---------------------------------------------------------------------------
# QA additions — pure WebRTC/TURN classifiers (no live signaling/TURN needed)
# ---------------------------------------------------------------------------


class TestWebrtcSignalingHealth:
    def test_down_when_transport_inactive(self):
        # Even a stale "connected" flag can't make it ok if transport is dead.
        assert webrtc_signaling_health(webrtc_active=False, signaling_connected=True) == "down"

    def test_degraded_when_active_but_signaling_off(self):
        assert webrtc_signaling_health(webrtc_active=True, signaling_connected=False) == "degraded"

    def test_ok_when_active_and_connected(self):
        assert webrtc_signaling_health(webrtc_active=True, signaling_connected=True) == "ok"


class TestTurnSecret:
    def test_present_when_set(self):
        assert turn_secret_present({"SKCOMMS_TURN_SECRET": "s3cr3t"}) is True

    def test_absent_when_blank(self):
        assert turn_secret_present({"SKCOMMS_TURN_SECRET": "   "}) is False

    def test_absent_when_missing(self):
        assert turn_secret_present({}) is False

    def test_warning_none_when_present(self):
        assert webrtc_turn_warning({"SKCOMMS_TURN_SECRET": "x"}) is None

    def test_warning_string_when_missing(self):
        warn = webrtc_turn_warning({})
        assert warn is not None
        assert "SKCOMMS_TURN_SECRET" in warn


# ---------------------------------------------------------------------------
# QA additions — _route_file_message dispatch + init-helper non-fatal behaviour
# ---------------------------------------------------------------------------


class TestRouteFileMessage:
    def test_no_file_service_returns_false(self):
        from skchat.models import ChatMessage

        daemon = ChatDaemon(interval=5, quiet=True)
        daemon._file_service = None
        msg = ChatMessage(sender="a", recipient="b", content='{"type": "FILE_TRANSFER_INIT"}')
        assert daemon._route_file_message(msg) is False

    def test_plain_chat_message_not_routed(self):
        from skchat.models import ChatMessage

        daemon = ChatDaemon(interval=5, quiet=True)
        daemon._file_service = MagicMock()
        msg = ChatMessage(sender="a", recipient="b", content="just a normal message")
        assert daemon._route_file_message(msg) is False
        daemon._file_service.store_incoming_chunk.assert_not_called()

    def test_file_transfer_init_routed_to_service(self):
        from skchat.models import ChatMessage

        daemon = ChatDaemon(interval=5, quiet=True)
        fs = MagicMock()
        daemon._file_service = fs
        msg = ChatMessage(
            sender="capauth:peer@x",
            recipient="capauth:me@x",
            content='{"type": "FILE_TRANSFER_INIT", "transfer_id": "t1"}',
        )
        assert daemon._route_file_message(msg) is True
        fs.store_incoming_chunk.assert_called_once()
        # The envelope sender is carried through for attribution.
        payload = fs.store_incoming_chunk.call_args[0][0]
        assert payload["sender"] == "capauth:peer@x"

    def test_marker_present_but_wrong_type_not_routed(self):
        """Content mentions FILE_CHUNK but JSON type is unknown → not routed."""
        from skchat.models import ChatMessage

        daemon = ChatDaemon(interval=5, quiet=True)
        fs = MagicMock()
        daemon._file_service = fs
        msg = ChatMessage(
            sender="a", recipient="b", content='{"type": "SOMETHING_ELSE", "note": "FILE_CHUNK"}'
        )
        assert daemon._route_file_message(msg) is False
        fs.store_incoming_chunk.assert_not_called()

    def test_service_exception_still_consumes_message(self):
        """A store_incoming_chunk failure is swallowed but the msg is consumed."""
        from skchat.models import ChatMessage

        daemon = ChatDaemon(interval=5, quiet=True)
        fs = MagicMock()
        fs.store_incoming_chunk.side_effect = RuntimeError("disk full")
        daemon._file_service = fs
        msg = ChatMessage(sender="a", recipient="b", content='{"type": "FILE_CHUNK", "seq": 1}')
        # Returns True (recognised + handed off) even though the store raised.
        assert daemon._route_file_message(msg) is True


class TestInitHelpersNonFatal:
    def test_init_reaper_non_fatal(self):
        """A reaper init failure returns None, not an exception."""
        daemon = ChatDaemon(interval=5, quiet=True)
        bad_history = MagicMock()
        with patch("skchat.ephemeral.MessageReaper", side_effect=RuntimeError("boom")):
            assert daemon._init_reaper(bad_history) is None

    def test_init_memory_bridge_non_fatal(self):
        daemon = ChatDaemon(interval=5, quiet=True)
        with patch("skchat.memory_bridge.MemoryBridge", side_effect=RuntimeError("boom")):
            assert daemon._init_memory_bridge(MagicMock()) is None


class TestRootLoggingRotation:
    """A1 (F1): daemon.log must be size-capped via RotatingFileHandler, not an
    unbounded FileHandler. Level + rotation knobs are env-tunable."""

    @staticmethod
    def _reset_root_handlers():
        import logging

        root = logging.getLogger()
        saved_handlers = root.handlers[:]
        saved_level = root.level
        root.handlers = []
        return root, saved_handlers, saved_level

    def test_log_file_installs_rotating_handler_with_defaults(self, tmp_path, monkeypatch):
        import logging
        import logging.handlers

        monkeypatch.delenv("SKCHAT_LOG_LEVEL", raising=False)
        monkeypatch.delenv("SKCHAT_LOG_MAX_BYTES", raising=False)
        monkeypatch.delenv("SKCHAT_LOG_BACKUP_COUNT", raising=False)

        root, saved_handlers, saved_level = self._reset_root_handlers()
        try:
            log_file = tmp_path / "daemon.log"
            ChatDaemon(interval=5, log_file=log_file, quiet=True)

            rotating = [
                h
                for h in root.handlers
                if isinstance(h, logging.handlers.RotatingFileHandler)
            ]
            assert rotating, "expected a RotatingFileHandler on the root logger"
            handler = rotating[0]
            # Defaults from the plan: 50 MB cap, 5 backups.
            assert handler.maxBytes == 50_000_000
            assert handler.backupCount == 5
            # Default level INFO.
            assert root.level == logging.INFO
        finally:
            for h in root.handlers:
                try:
                    h.close()
                except Exception:
                    pass
            root.handlers = saved_handlers
            root.setLevel(saved_level)

    def test_log_level_env_override(self, tmp_path, monkeypatch):
        import logging

        monkeypatch.setenv("SKCHAT_LOG_LEVEL", "DEBUG")
        root, saved_handlers, saved_level = self._reset_root_handlers()
        try:
            ChatDaemon(interval=5, log_file=tmp_path / "daemon.log", quiet=True)
            assert root.level == logging.DEBUG
        finally:
            for h in root.handlers:
                try:
                    h.close()
                except Exception:
                    pass
            root.handlers = saved_handlers
            root.setLevel(saved_level)

    def test_rotation_size_and_backup_env_override(self, tmp_path, monkeypatch):
        import logging
        import logging.handlers

        monkeypatch.setenv("SKCHAT_LOG_MAX_BYTES", "1234567")
        monkeypatch.setenv("SKCHAT_LOG_BACKUP_COUNT", "9")
        root, saved_handlers, saved_level = self._reset_root_handlers()
        try:
            ChatDaemon(interval=5, log_file=tmp_path / "daemon.log", quiet=True)
            rotating = [
                h
                for h in root.handlers
                if isinstance(h, logging.handlers.RotatingFileHandler)
            ]
            assert rotating
            assert rotating[0].maxBytes == 1234567
            assert rotating[0].backupCount == 9
        finally:
            for h in root.handlers:
                try:
                    h.close()
                except Exception:
                    pass
            root.handlers = saved_handlers
            root.setLevel(saved_level)

    def test_no_log_file_installs_no_rotating_handler(self, monkeypatch):
        import logging
        import logging.handlers

        root, saved_handlers, saved_level = self._reset_root_handlers()
        try:
            ChatDaemon(interval=5, quiet=True)  # no log_file
            rotating = [
                h
                for h in root.handlers
                if isinstance(h, logging.handlers.RotatingFileHandler)
            ]
            assert rotating == []
        finally:
            root.handlers = saved_handlers
            root.setLevel(saved_level)


class TestRootLoggingRotationClamp:
    """F2 (unbounded-growth regression): a hostile/degenerate rotation env
    (SKCHAT_LOG_MAX_BYTES=0/-1/garbage, SKCHAT_LOG_BACKUP_COUNT=0) must NOT
    silently re-create the unbounded log. The values are clamped to a sane
    floor so the RotatingFileHandler always actually rotates."""

    @staticmethod
    def _reset_root_handlers():
        import logging

        root = logging.getLogger()
        saved_handlers = root.handlers[:]
        saved_level = root.level
        root.handlers = []
        return root, saved_handlers, saved_level

    @pytest.mark.parametrize("max_bytes_env", ["0", "-1", "not-a-number", ""])
    @pytest.mark.parametrize("backup_env", ["0", "-1", "garbage"])
    def test_degenerate_env_still_rotates_and_caps(
        self, tmp_path, monkeypatch, max_bytes_env, backup_env
    ):
        import logging
        import logging.handlers

        monkeypatch.setenv("SKCHAT_LOG_MAX_BYTES", max_bytes_env)
        monkeypatch.setenv("SKCHAT_LOG_BACKUP_COUNT", backup_env)
        root, saved_handlers, saved_level = self._reset_root_handlers()
        try:
            ChatDaemon(interval=5, log_file=tmp_path / "daemon.log", quiet=True)
            rotating = [
                h
                for h in root.handlers
                if isinstance(h, logging.handlers.RotatingFileHandler)
            ]
            assert rotating, "expected a RotatingFileHandler even for degenerate env"
            handler = rotating[0]
            # A maxBytes of 0 disables rotation entirely (unbounded). Must be
            # clamped to a real, non-zero floor.
            assert handler.maxBytes >= 1_000_000
            # A backupCount of 0 means RotatingFileHandler truncates instead of
            # keeping backups — at least one backup must survive.
            assert handler.backupCount >= 1
        finally:
            for h in root.handlers:
                try:
                    h.close()
                except Exception:
                    pass
            root.handlers = saved_handlers
            root.setLevel(saved_level)


class TestRootLoggingThirdPartyFilter:
    """F3 (DEBUG firehose): at SKCHAT_LOG_LEVEL=DEBUG the rotating file handler
    must not pull in third-party library DEBUG noise (urllib3, asyncio, pgpy,
    …). A filter restricts the file handler to the skchat/skcomms logger trees
    (plus root records emitted directly)."""

    @staticmethod
    def _reset_root_handlers():
        import logging

        root = logging.getLogger()
        saved_handlers = root.handlers[:]
        saved_level = root.level
        root.handlers = []
        return root, saved_handlers, saved_level

    def _make_record(self, name, level):
        import logging

        return logging.LogRecord(
            name=name,
            level=level,
            pathname=__file__,
            lineno=1,
            msg="x",
            args=(),
            exc_info=None,
        )

    def test_debug_filters_third_party_but_passes_skchat(self, tmp_path, monkeypatch):
        import logging
        import logging.handlers

        monkeypatch.setenv("SKCHAT_LOG_LEVEL", "DEBUG")
        root, saved_handlers, saved_level = self._reset_root_handlers()
        try:
            ChatDaemon(interval=5, log_file=tmp_path / "daemon.log", quiet=True)
            rotating = [
                h
                for h in root.handlers
                if isinstance(h, logging.handlers.RotatingFileHandler)
            ]
            assert rotating
            handler = rotating[0]

            # Third-party DEBUG noise is filtered OUT.
            noise = self._make_record("urllib3.connectionpool", logging.DEBUG)
            assert handler.filter(noise) is False or handler.filter(noise) == 0

            # skchat's own DEBUG records pass THROUGH.
            ours = self._make_record("skchat.daemon", logging.DEBUG)
            assert handler.filter(ours)

            # skcomms transport records also pass.
            transport = self._make_record("skcomms.router", logging.DEBUG)
            assert handler.filter(transport)

            # Root records emitted directly still pass.
            root_rec = self._make_record("root", logging.INFO)
            assert handler.filter(root_rec)
        finally:
            for h in root.handlers:
                try:
                    h.close()
                except Exception:
                    pass
            root.handlers = saved_handlers
            root.setLevel(saved_level)


class TestOutboxSummaryLogLevel:
    """F4 (lost error signal): the per-cycle Outbox summary must surface at INFO
    when deliveries failed (chronic failures were hidden at DEBUG), and stay at
    DEBUG on the all-clear path so it isn't per-cycle noise."""

    def test_level_is_info_when_failures(self):
        from skchat.daemon import _outbox_summary_level

        assert _outbox_summary_level(failed=1) == "info"
        assert _outbox_summary_level(failed=42) == "info"

    def test_level_is_debug_when_no_failures(self):
        from skchat.daemon import _outbox_summary_level

        assert _outbox_summary_level(failed=0) == "debug"

    def test_daemon_log_emits_summary_at_info_on_failure(self, tmp_path, caplog):
        import logging

        from skchat.daemon import _outbox_summary_level

        daemon = ChatDaemon(interval=5, quiet=True)
        delivered, failed = 2, 3
        with caplog.at_level(logging.INFO, logger="skchat.daemon"):
            daemon._log(
                f"Outbox: {delivered} delivered, {failed} retried/failed",
                _outbox_summary_level(failed=failed),
            )
        matching = [
            r
            for r in caplog.records
            if "Outbox:" in r.getMessage() and r.levelno == logging.INFO
        ]
        assert matching, "expected the Outbox summary at INFO when failed>0"


class TestWebRTCSignalingHealthDedup:
    """A2 (F3-skchat): _write_daemon_stats must WARN on WebRTC signaling-health
    *transitions* only, not once per ~30s cycle."""

    @staticmethod
    def _daemon_with_stats(tmp_path, monkeypatch):
        from datetime import datetime, timezone

        stats_file = tmp_path / "daemon-stats.json"
        monkeypatch.setattr("skchat.daemon._DAEMON_STATS_FILE", stats_file)
        daemon = ChatDaemon(interval=5, quiet=True)
        daemon._webrtc_active = True
        daemon.start_time = datetime.now(timezone.utc)
        return daemon

    @staticmethod
    def _skcomms_with_webrtc(connected: bool):
        webrtc_t = MagicMock()
        webrtc_t.name = "webrtc"
        webrtc_t._signaling_connected = connected
        skcomms = MagicMock()
        skcomms.router.transports = [webrtc_t]
        return skcomms, webrtc_t

    def test_degraded_warns_once_across_repeated_cycles(self, tmp_path, monkeypatch, caplog):
        import logging

        daemon = self._daemon_with_stats(tmp_path, monkeypatch)
        skcomms, _ = self._skcomms_with_webrtc(connected=False)  # degraded

        with caplog.at_level(logging.WARNING, logger="skchat.daemon"):
            for _ in range(4):
                daemon._write_daemon_stats(watchdog=None, presence=None, skcomms=skcomms)

        warns = [r for r in caplog.records if "WebRTC signaling" in r.getMessage()]
        assert len(warns) == 1, "degraded state should warn only on the first transition"

    def test_recovery_then_redegrade_warns_again(self, tmp_path, monkeypatch, caplog):
        import logging

        daemon = self._daemon_with_stats(tmp_path, monkeypatch)
        skcomms, webrtc_t = self._skcomms_with_webrtc(connected=False)  # degraded

        with caplog.at_level(logging.WARNING, logger="skchat.daemon"):
            daemon._write_daemon_stats(watchdog=None, presence=None, skcomms=skcomms)  # warn 1
            daemon._write_daemon_stats(watchdog=None, presence=None, skcomms=skcomms)  # no warn
            webrtc_t._signaling_connected = True  # recover → ok
            daemon._write_daemon_stats(watchdog=None, presence=None, skcomms=skcomms)  # no warn
            webrtc_t._signaling_connected = False  # degrade again → new transition
            daemon._write_daemon_stats(watchdog=None, presence=None, skcomms=skcomms)  # warn 2

        warns = [r for r in caplog.records if "WebRTC signaling" in r.getMessage()]
        assert len(warns) == 2, "a fresh degrade after recovery is a new transition"
