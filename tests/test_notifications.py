"""Tests for DesktopNotifier."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from skchat.notifications import (
    DesktopNotifier,
    NotificationLevel,
    desktop_notifications_enabled,
)


@pytest.fixture(autouse=True)
def _enable_desktop_notify(monkeypatch):
    """This module exercises the notify dispatch path itself, so re-enable the
    SK_DESKTOP_NOTIFY gate that conftest defaults off for the rest of the suite.
    subprocess.run is mocked in every test here, so nothing real ever fires."""
    monkeypatch.setenv("SK_DESKTOP_NOTIFY", "1")


@pytest.fixture
def notifier_available():
    """DesktopNotifier with notify-send marked available."""
    with patch("skchat.notifications.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        n = DesktopNotifier()
        assert n.available is True
        yield n, mock_run


@pytest.fixture
def notifier_unavailable():
    """DesktopNotifier with notify-send absent."""
    with patch("skchat.notifications.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1)
        n = DesktopNotifier()
        assert n.available is False
        yield n, mock_run


# ---------------------------------------------------------------------------
# Test 1: notify-send availability detection
# ---------------------------------------------------------------------------
def test_check_available_true():
    with patch("skchat.notifications.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        n = DesktopNotifier()
    assert n.available is True
    mock_run.assert_called_once_with(["which", "notify-send"], capture_output=True)


def test_check_available_false():
    with patch("skchat.notifications.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1)
        n = DesktopNotifier()
    assert n.available is False


# ---------------------------------------------------------------------------
# Test 2: notify() builds correct command
# ---------------------------------------------------------------------------
def test_notify_command_args(notifier_available):
    n, mock_run = notifier_available
    mock_run.reset_mock()
    mock_run.return_value = MagicMock(returncode=0)

    result = n.notify("Hello", "World", urgency="normal", icon="dialog-info", timeout_ms=3000)

    assert result is True
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "notify-send"
    assert "--urgency" in cmd
    assert "normal" in cmd
    assert "--icon" in cmd
    assert "dialog-info" in cmd
    assert "--expire-time" in cmd
    assert "3000" in cmd
    assert "Hello" in cmd
    assert "World" in cmd


# ---------------------------------------------------------------------------
# Test 3: notify() returns False when unavailable (no subprocess called)
# ---------------------------------------------------------------------------
def test_notify_skips_when_unavailable(notifier_unavailable):
    n, mock_run = notifier_unavailable
    mock_run.reset_mock()

    result = n.notify("title", "body")

    assert result is False
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: notify_message() uses critical urgency for mentions
# ---------------------------------------------------------------------------
def test_notify_message_mention_urgency(notifier_available):
    n, mock_run = notifier_available
    mock_run.reset_mock()
    mock_run.return_value = MagicMock(returncode=0)

    n.notify_message("alice", "hey @opus check this", is_mention=True)

    cmd = mock_run.call_args[0][0]
    urgency_idx = cmd.index("--urgency") + 1
    assert cmd[urgency_idx] == NotificationLevel.CRITICAL.value


def test_notify_message_normal_urgency(notifier_available):
    n, mock_run = notifier_available
    mock_run.reset_mock()
    mock_run.return_value = MagicMock(returncode=0)

    n.notify_message("bob", "just saying hi", is_mention=False)

    cmd = mock_run.call_args[0][0]
    urgency_idx = cmd.index("--urgency") + 1
    assert cmd[urgency_idx] == NotificationLevel.NORMAL.value


# ---------------------------------------------------------------------------
# Test 5: notify_message() title includes sender name
# ---------------------------------------------------------------------------
def test_notify_message_title_contains_sender(notifier_available):
    n, mock_run = notifier_available
    mock_run.reset_mock()
    mock_run.return_value = MagicMock(returncode=0)

    n.notify_message("lumina", "hello")

    cmd = mock_run.call_args[0][0]
    assert any("lumina" in str(arg) for arg in cmd)


# ---------------------------------------------------------------------------
# Test 6: notify_lumina() uses 10-second timeout
# ---------------------------------------------------------------------------
def test_notify_lumina_timeout(notifier_available):
    n, mock_run = notifier_available
    mock_run.reset_mock()
    mock_run.return_value = MagicMock(returncode=0)

    n.notify_lumina("a lumina message")

    cmd = mock_run.call_args[0][0]
    expire_idx = cmd.index("--expire-time") + 1
    assert cmd[expire_idx] == "10000"


# ---------------------------------------------------------------------------
# Test 7: notify() returns False when notify-send exits non-zero
# ---------------------------------------------------------------------------
def test_notify_returns_false_on_failure(notifier_available):
    n, mock_run = notifier_available
    mock_run.reset_mock()
    mock_run.return_value = MagicMock(returncode=1)

    result = n.notify("title", "body")

    assert result is False


# ---------------------------------------------------------------------------
# Test 8: NotificationLevel enum values
# ---------------------------------------------------------------------------
def test_notification_level_values():
    assert NotificationLevel.LOW.value == "low"
    assert NotificationLevel.NORMAL.value == "normal"
    assert NotificationLevel.CRITICAL.value == "critical"


# ---------------------------------------------------------------------------
# QA additions — the SK_DESKTOP_NOTIFY gate + lumina notification
# ---------------------------------------------------------------------------


class TestDesktopNotificationsEnabled:
    """The env gate that conftest uses to silence the whole suite."""

    def test_unset_defaults_enabled(self, monkeypatch):
        monkeypatch.delenv("SK_DESKTOP_NOTIFY", raising=False)
        assert desktop_notifications_enabled() is True

    def test_explicit_one_enabled(self, monkeypatch):
        monkeypatch.setenv("SK_DESKTOP_NOTIFY", "1")
        assert desktop_notifications_enabled() is True

    def test_falsey_values_disabled(self, monkeypatch):
        for val in ("0", "false", "no", "off", "", "  OFF  "):
            monkeypatch.setenv("SK_DESKTOP_NOTIFY", val)
            assert desktop_notifications_enabled() is False, val


def test_notify_suppressed_when_gate_off_even_if_available(monkeypatch):
    """Even with notify-send present, a falsey SK_DESKTOP_NOTIFY suppresses dispatch."""
    monkeypatch.setenv("SK_DESKTOP_NOTIFY", "1")
    with patch("skchat.notifications.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        n = DesktopNotifier()
        assert n.available is True
        mock_run.reset_mock()
        # Now flip the gate off and confirm notify() short-circuits.
        monkeypatch.setenv("SK_DESKTOP_NOTIFY", "0")
        result = n.notify("title", "body")
    assert result is False
    mock_run.assert_not_called()


def test_check_available_false_when_gate_off(monkeypatch):
    """A disabled gate makes the notifier report unavailable without probing PATH."""
    monkeypatch.setenv("SK_DESKTOP_NOTIFY", "0")
    with patch("skchat.notifications.subprocess.run") as mock_run:
        n = DesktopNotifier()
    assert n.available is False
    mock_run.assert_not_called()


def test_notify_lumina_title_has_marker(notifier_available):
    """notify_lumina uses the purple-heart Lumina title."""
    n, mock_run = notifier_available
    mock_run.reset_mock()
    mock_run.return_value = MagicMock(returncode=0)
    n.notify_lumina("hi")
    cmd = mock_run.call_args[0][0]
    assert any("Lumina" in str(arg) for arg in cmd)


def test_custom_app_name_stored():
    """A custom app_name is retained on the notifier."""
    with patch("skchat.notifications.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        n = DesktopNotifier(app_name="MyApp")
    assert n.app_name == "MyApp"
