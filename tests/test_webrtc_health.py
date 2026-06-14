"""Tests for WebRTC signaling health classification + TURN-secret check.

These are pure helpers: no live WebRTC/TURN/STUN servers are required. They
let the daemon surface a "signaling down / degraded" state and warn when the
TURN shared secret is missing — without ever failing the daemon (the daemon
must keep running with local fallback regardless).
"""

from __future__ import annotations

from skchat.daemon import (
    turn_secret_present,
    webrtc_signaling_health,
    webrtc_turn_warning,
)


class TestWebRTCSignalingHealth:
    def test_ok_when_active_and_signaling_connected(self) -> None:
        assert (
            webrtc_signaling_health(webrtc_active=True, signaling_connected=True)
            == "ok"
        )

    def test_degraded_when_active_but_signaling_down(self) -> None:
        # Transport is wired (LAN / local fallback possible) but the
        # signaling server is unreachable — relayed calls won't connect.
        assert (
            webrtc_signaling_health(webrtc_active=True, signaling_connected=False)
            == "degraded"
        )

    def test_down_when_transport_not_active(self) -> None:
        assert (
            webrtc_signaling_health(webrtc_active=False, signaling_connected=False)
            == "down"
        )
        # Even a stale "connected" flag is "down" when the transport isn't wired.
        assert (
            webrtc_signaling_health(webrtc_active=False, signaling_connected=True)
            == "down"
        )


class TestTurnSecretPresence:
    def test_present_when_env_set(self) -> None:
        assert turn_secret_present({"SKCOMMS_TURN_SECRET": "hunter2"}) is True

    def test_absent_when_env_missing(self) -> None:
        assert turn_secret_present({}) is False

    def test_absent_when_env_blank(self) -> None:
        assert turn_secret_present({"SKCOMMS_TURN_SECRET": "   "}) is False


class TestTurnWarning:
    def test_warns_when_secret_missing(self) -> None:
        warning = webrtc_turn_warning({})
        assert warning is not None
        assert "TURN" in warning

    def test_no_warning_when_secret_present(self) -> None:
        assert webrtc_turn_warning({"SKCOMMS_TURN_SECRET": "hunter2"}) is None
