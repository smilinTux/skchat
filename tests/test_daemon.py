"""Tests for chat daemon — background message receiving."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skchat.daemon import ChatDaemon, DaemonShutdown, run_daemon
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


@patch("skchat.transport.ChatTransport")
@patch("skchat.history.ChatHistory")
@patch("skchat.identity_bridge.get_sovereign_identity")
def test_daemon_start_no_messages(
    mock_identity,
    mock_history_class,
    mock_transport_class,
    mock_transport,
):
    """Test daemon with no incoming messages."""
    mock_transport_class.from_config.return_value = mock_transport
    mock_identity.return_value = "capauth:test@capauth.local"
    
    daemon = ChatDaemon(interval=0.1, quiet=True)
    
    def stop_after_polls():
        time.sleep(0.3)
        daemon.running = False
    
    import threading
    stop_thread = threading.Thread(target=stop_after_polls)
    stop_thread.start()
    
    daemon.start()
    stop_thread.join()
    
    assert daemon.poll_count >= 2
    assert daemon.total_received == 0


@patch("skchat.transport.ChatTransport")
@patch("skchat.history.ChatHistory")
@patch("skchat.identity_bridge.get_sovereign_identity")
def test_daemon_start_with_messages(
    mock_identity,
    mock_history_class,
    mock_transport_class,
    mock_transport,
    sample_message,
):
    """Test daemon receiving messages."""
    mock_transport.poll_inbox = MagicMock(return_value=[sample_message])
    mock_transport_class.from_config.return_value = mock_transport
    mock_identity.return_value = "capauth:test@capauth.local"
    
    daemon = ChatDaemon(interval=0.1, quiet=True)
    
    def stop_after_polls():
        time.sleep(0.3)
        daemon.running = False
    
    import threading
    stop_thread = threading.Thread(target=stop_after_polls)
    stop_thread.start()
    
    daemon.start()
    stop_thread.join()
    
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


@patch("skchat.transport.ChatTransport")
@patch("skchat.history.ChatHistory")
@patch("skchat.identity_bridge.get_sovereign_identity")
def test_daemon_poll_error_handling(
    mock_identity,
    mock_history_class,
    mock_transport_class,
    mock_transport,
):
    """Test daemon handling poll errors gracefully."""
    mock_transport.poll_inbox = MagicMock(side_effect=Exception("Transport error"))
    mock_transport_class.from_config.return_value = mock_transport
    mock_identity.return_value = "capauth:test@capauth.local"
    
    daemon = ChatDaemon(interval=0.1, quiet=True)
    
    def stop_after_polls():
        time.sleep(0.3)
        daemon.running = False
    
    import threading
    stop_thread = threading.Thread(target=stop_after_polls)
    stop_thread.start()
    
    daemon.start()
    stop_thread.join()
    
    assert daemon.poll_count >= 2


@patch("skchat.transport.ChatTransport")
def test_daemon_start_transport_init_failure(mock_transport_class):
    """Test daemon handling transport initialization failure."""
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
    with patch.dict("os.environ", {
        "SKCHAT_DAEMON_INTERVAL": "10.0",
        "SKCHAT_DAEMON_LOG": "/tmp/daemon.log",
        "SKCHAT_DAEMON_QUIET": "true",
    }):
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


@patch("skchat.daemon.ChatDaemon")
def test_run_daemon(mock_daemon_class):
    """Test run_daemon wrapper function."""
    mock_daemon = MagicMock()
    mock_daemon_class.return_value = mock_daemon
    
    run_daemon(interval=10, log_file="/tmp/test.log", quiet=True)
    
    mock_daemon_class.assert_called_once()
    mock_daemon.start.assert_called_once()


@patch("skchat.daemon.ChatDaemon")
def test_run_daemon_with_exception(mock_daemon_class):
    """Test run_daemon handling exceptions."""
    mock_daemon = MagicMock()
    mock_daemon.start.side_effect = Exception("Daemon error")
    mock_daemon_class.return_value = mock_daemon
    
    with pytest.raises(SystemExit):
        run_daemon()
