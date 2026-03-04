"""Tests for skchat.watchdog.TransportWatchdog.

Covers:
- ping_skcomm: success, non-200, connection error
- check(): reconnect trigger, single-trigger-per-streak
- transport_status / is_healthy properties
- uptime_seconds
- check_webrtc(): signaling up, signaling down, ICE parsing, peer count
- health_summary() integration
- Fallback transport detection via transport.py
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from skchat.watchdog import TransportWatchdog


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_transport():
    t = MagicMock()
    t.reconnect.return_value = None
    return t


@pytest.fixture
def watchdog(mock_transport):
    return TransportWatchdog(
        transport=mock_transport,
        skcomm_url="http://127.0.0.1:9384",
        failure_threshold=3,
    )


# ---------------------------------------------------------------------------
# ping_skcomm
# ---------------------------------------------------------------------------


class TestPingSkcomm:
    def test_success_resets_failures(self, watchdog):
        """HTTP 200 resets consecutive_failures and returns True."""
        resp = MagicMock(status_code=200)
        with patch("httpx.get", return_value=resp):
            watchdog.consecutive_failures = 2
            result = watchdog.ping_skcomm()

        assert result is True
        assert watchdog.consecutive_failures == 0
        assert watchdog.last_success_at is not None
        assert watchdog._reconnect_pending is False

    def test_non_200_increments_failures(self, watchdog):
        """HTTP 503 increments consecutive_failures and returns False."""
        resp = MagicMock(status_code=503)
        with patch("httpx.get", return_value=resp):
            result = watchdog.ping_skcomm()

        assert result is False
        assert watchdog.consecutive_failures == 1
        assert watchdog.last_failure_at is not None

    def test_connection_error_increments_failures(self, watchdog):
        """Connection error increments consecutive_failures and returns False."""
        with patch("httpx.get", side_effect=ConnectionError("refused")):
            result = watchdog.ping_skcomm()

        assert result is False
        assert watchdog.consecutive_failures == 1

    def test_timeout_error_increments_failures(self, watchdog):
        """Timeout is treated as a failure."""
        import httpx

        with patch("httpx.get", side_effect=httpx.TimeoutException("timeout")):
            result = watchdog.ping_skcomm()

        assert result is False
        assert watchdog.consecutive_failures == 1


# ---------------------------------------------------------------------------
# check() — reconnect logic
# ---------------------------------------------------------------------------


class TestCheck:
    def test_check_healthy(self, watchdog):
        """check() returns True when ping succeeds."""
        resp = MagicMock(status_code=200)
        with patch("httpx.get", return_value=resp):
            assert watchdog.check() is True

    def test_reconnect_triggered_at_threshold(self, watchdog, mock_transport):
        """reconnect() is called once consecutive failures reach threshold."""
        with patch("httpx.get", side_effect=ConnectionError("down")):
            for _ in range(3):
                watchdog.check()

        mock_transport.reconnect.assert_called_once()

    def test_reconnect_not_triggered_before_threshold(self, watchdog, mock_transport):
        """reconnect() is NOT called below the threshold."""
        with patch("httpx.get", side_effect=ConnectionError("down")):
            for _ in range(2):
                watchdog.check()

        mock_transport.reconnect.assert_not_called()

    def test_reconnect_fires_only_once_per_streak(self, watchdog, mock_transport):
        """reconnect() is called only once per failure streak, not on every extra failure."""
        with patch("httpx.get", side_effect=ConnectionError("down")):
            for _ in range(6):  # twice the threshold
                watchdog.check()

        mock_transport.reconnect.assert_called_once()

    def test_reconnect_rearmed_after_recovery(self, watchdog, mock_transport):
        """After recovery, a new failure streak can trigger another reconnect."""
        fail_resp = MagicMock(status_code=503)
        ok_resp = MagicMock(status_code=200)

        with patch("httpx.get", return_value=fail_resp):
            for _ in range(3):
                watchdog.check()

        assert mock_transport.reconnect.call_count == 1

        # Recover
        with patch("httpx.get", return_value=ok_resp):
            watchdog.check()

        assert watchdog._reconnect_pending is False

        # Fail again
        with patch("httpx.get", return_value=fail_resp):
            for _ in range(3):
                watchdog.check()

        assert mock_transport.reconnect.call_count == 2


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestProperties:
    def test_transport_status_healthy(self, watchdog):
        watchdog.consecutive_failures = 0
        assert watchdog.transport_status == "healthy"
        assert watchdog.is_healthy is True

    def test_transport_status_degraded(self, watchdog):
        watchdog.consecutive_failures = 1
        assert watchdog.transport_status == "degraded"
        assert watchdog.is_healthy is False

    def test_transport_status_unreachable(self, watchdog):
        watchdog.consecutive_failures = 3
        assert watchdog.transport_status == "unreachable"

    def test_uptime_increases_over_time(self, watchdog):
        """uptime_seconds increases as wall-clock time passes."""
        t0 = watchdog.uptime_seconds
        time.sleep(0.05)
        t1 = watchdog.uptime_seconds
        assert t1 > t0

    def test_uptime_starts_near_zero(self, watchdog):
        """A freshly-created watchdog has a very small uptime."""
        assert watchdog.uptime_seconds < 5.0


# ---------------------------------------------------------------------------
# check_webrtc()
# ---------------------------------------------------------------------------


class TestCheckWebrtc:
    def test_signaling_ok_on_426(self, watchdog):
        """HTTP 426 Upgrade Required means the WebSocket endpoint is up."""
        ws_resp = MagicMock(status_code=426)
        ice_resp = MagicMock(status_code=200)
        ice_resp.json.return_value = {"ice_servers": [], "active_peers": 0}

        with patch("httpx.get", side_effect=[ws_resp, ice_resp]):
            result = watchdog.check_webrtc()

        assert result["signaling_ok"] is True

    def test_signaling_down_on_connection_error(self, watchdog):
        """Connection refused means signaling is down."""
        with patch("httpx.get", side_effect=ConnectionError("refused")):
            result = watchdog.check_webrtc()

        assert result["signaling_ok"] is False
        assert result["ice_servers_configured"] is False
        assert result["active_peers"] == 0

    def test_ice_servers_configured_when_present(self, watchdog):
        """ICE servers list is non-empty → ice_servers_configured True."""
        ws_resp = MagicMock(status_code=426)
        ice_resp = MagicMock(status_code=200)
        ice_resp.json.return_value = {
            "ice_servers": [{"urls": "stun:stun.example.com"}],
            "active_peers": 2,
        }

        with patch("httpx.get", side_effect=[ws_resp, ice_resp]):
            result = watchdog.check_webrtc()

        assert result["ice_servers_configured"] is True
        assert result["active_peers"] == 2

    def test_ice_servers_empty_list(self, watchdog):
        """Empty ice_servers list → ice_servers_configured False."""
        ws_resp = MagicMock(status_code=426)
        ice_resp = MagicMock(status_code=200)
        ice_resp.json.return_value = {"ice_servers": [], "active_peers": 0}

        with patch("httpx.get", side_effect=[ws_resp, ice_resp]):
            result = watchdog.check_webrtc()

        assert result["ice_servers_configured"] is False

    def test_ice_endpoint_non_200_ignored(self, watchdog):
        """Non-200 from ICE endpoint → defaults; signaling status unaffected."""
        ws_resp = MagicMock(status_code=426)
        ice_resp = MagicMock(status_code=500)

        with patch("httpx.get", side_effect=[ws_resp, ice_resp]):
            result = watchdog.check_webrtc()

        assert result["signaling_ok"] is True
        assert result["ice_servers_configured"] is False
        assert result["active_peers"] == 0

    def test_camelcase_ice_servers_field(self, watchdog):
        """iceServers (camelCase) is also accepted."""
        ws_resp = MagicMock(status_code=200)
        ice_resp = MagicMock(status_code=200)
        ice_resp.json.return_value = {
            "iceServers": [{"urls": "turn:turn.example.com"}],
            "activePeers": 5,
        }

        with patch("httpx.get", side_effect=[ws_resp, ice_resp]):
            result = watchdog.check_webrtc()

        assert result["ice_servers_configured"] is True
        assert result["active_peers"] == 5


# ---------------------------------------------------------------------------
# health_summary()
# ---------------------------------------------------------------------------


class TestHealthSummary:
    def test_summary_keys_present(self, watchdog):
        """health_summary returns all expected keys."""
        resp = MagicMock(status_code=200)
        ws_resp = MagicMock(status_code=426)
        ice_resp = MagicMock(status_code=200)
        ice_resp.json.return_value = {"ice_servers": [], "active_peers": 0}

        with patch("httpx.get", side_effect=[resp, ws_resp, ice_resp]):
            summary = watchdog.health_summary()

        assert "skcomm_ok" in summary
        assert "transport_status" in summary
        assert "webrtc" in summary
        assert "file_transport_available" in summary
        assert "uptime_seconds" in summary
        assert "consecutive_failures" in summary

    def test_file_transport_always_available(self, watchdog):
        """file_transport_available is always True."""
        with patch("httpx.get", side_effect=ConnectionError("down")):
            summary = watchdog.health_summary()

        assert summary["file_transport_available"] is True

    def test_summary_skcomm_ok_when_healthy(self, watchdog):
        """skcomm_ok reflects ping_skcomm result."""
        ok_resp = MagicMock(status_code=200)
        ws_resp = MagicMock(status_code=426)
        ice_resp = MagicMock(status_code=503)

        with patch("httpx.get", side_effect=[ok_resp, ws_resp, ice_resp]):
            summary = watchdog.health_summary()

        assert summary["skcomm_ok"] is True


# ---------------------------------------------------------------------------
# Fallback transport (transport.py integration)
# ---------------------------------------------------------------------------


class TestFallbackTransport:
    def test_fallback_used_when_primary_fails(self):
        """When skcomm.send() raises, the fallback transport is tried."""
        from skchat.history import ChatHistory
        from skchat.models import ChatMessage, DeliveryStatus
        from skchat.transport import ChatTransport

        mock_primary = MagicMock()
        mock_primary.send.side_effect = ConnectionError("WebRTC down")

        mock_fallback = MagicMock()
        mock_fallback.send.return_value = MagicMock(delivered=True)

        mock_history = MagicMock()

        transport = ChatTransport(
            skcomm=mock_primary,
            history=mock_history,
            identity="capauth:test@skchat",
            fallback_transport=mock_fallback,
        )

        msg = ChatMessage(
            sender="capauth:test@skchat",
            recipient="capauth:lumina@skworld",
            content="fallback test",
        )

        result = transport.send_message(msg)

        mock_fallback.send.assert_called_once()
        assert result["transport"] == "file"
        assert result["delivered"] is True

    def test_no_fallback_returns_error(self):
        """Without a fallback, primary failure returns delivered=False."""
        from skchat.models import ChatMessage
        from skchat.transport import ChatTransport

        mock_primary = MagicMock()
        mock_primary.send.side_effect = ConnectionError("WebRTC down")
        mock_history = MagicMock()

        transport = ChatTransport(
            skcomm=mock_primary,
            history=mock_history,
            identity="capauth:test@skchat",
        )

        msg = ChatMessage(
            sender="capauth:test@skchat",
            recipient="capauth:lumina@skworld",
            content="no fallback",
        )

        result = transport.send_message(msg)

        assert result["delivered"] is False
        assert "error" in result
