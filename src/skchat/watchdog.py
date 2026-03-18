"""Transport watchdog -- monitors SKComm health and triggers reconnect.

Pings the SKComm HTTP health endpoint on each check cycle.  Consecutive
failures at or above the configured threshold trigger transport.reconnect()
to attempt recovery.  The failure counter resets on the next successful ping.

Typical usage from the daemon main loop (every ~30s)::

    watchdog = TransportWatchdog(transport=skcomm)
    # ... in loop ...
    watchdog.check()
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("skchat.watchdog")

_FAILURE_THRESHOLD = 3
_PING_TIMEOUT = 5.0


class TransportWatchdog:
    """Monitors SKComm transport health via HTTP ping.

    On each check() call, {skcomm_url}/health is polled with an
    HTTP GET (timeout=5s).  Consecutive failures at or above
    failure_threshold trigger transport.reconnect() -- once per failure
    streak (rearms on the next successful ping).

    Args:
        transport: Object with an optional reconnect() method.
            Typically the SKComm instance used by the daemon.
        skcomm_url: Base URL of the SKComm HTTP API.
        failure_threshold: Number of consecutive failures before reconnect
            is triggered.  Defaults to 3.
    """

    def __init__(
        self,
        transport: object,
        skcomm_url: str = "http://127.0.0.1:9384",
        failure_threshold: int = _FAILURE_THRESHOLD,
    ) -> None:
        self._transport = transport
        self._skcomm_base = skcomm_url.rstrip("/")
        self._health_url = f"{self._skcomm_base}/health"
        self._failure_threshold = failure_threshold
        self.consecutive_failures: int = 0
        self.last_success_at: Optional[datetime] = None
        self.last_failure_at: Optional[datetime] = None
        self._reconnect_pending: bool = False
        self._started_at: datetime = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ping_skcomm(self) -> bool:
        """Ping {skcomm_url}/health with a 5-second timeout.

        On HTTP 200:
          - consecutive_failures is reset to 0
          - last_success_at is updated
          - _reconnect_pending is cleared

        On any other outcome (connection error, non-200 status, timeout):
          - consecutive_failures is incremented
          - last_failure_at is updated

        Returns:
            bool: True if the endpoint responded with HTTP 200.
        """
        try:
            import httpx

            resp = httpx.get(self._health_url, timeout=_PING_TIMEOUT)
            if resp.status_code == 200:
                if self.consecutive_failures > 0:
                    logger.info(
                        "Watchdog: SKComm healthy again (was %d consecutive failures)",
                        self.consecutive_failures,
                    )
                self.consecutive_failures = 0
                self.last_success_at = datetime.now(timezone.utc)
                self._reconnect_pending = False
                return True
            logger.debug("Watchdog: SKComm health returned HTTP %d", resp.status_code)
        except Exception as exc:
            logger.debug("Watchdog: SKComm health ping error: %s", exc)

        self.consecutive_failures += 1
        self.last_failure_at = datetime.now(timezone.utc)
        return False

    def check(self) -> bool:
        """Run one watchdog cycle.

        Calls ping_skcomm().  When consecutive_failures reaches
        failure_threshold, calls transport.reconnect() once per streak.

        Returns:
            bool: True if SKComm is healthy.
        """
        ok = self.ping_skcomm()
        if ok:
            return True

        logger.warning(
            "Watchdog: SKComm health check failed (consecutive=%d/%d)",
            self.consecutive_failures,
            self._failure_threshold,
        )
        if self.consecutive_failures >= self._failure_threshold and not self._reconnect_pending:
            self._reconnect_pending = True
            self._trigger_reconnect()
        return False

    def check_webrtc(self) -> dict:
        """Check WebRTC signaling connectivity.

        Probes two endpoints:
        1. WebSocket signaling endpoint at {skcomm_url}/webrtc/ws — any HTTP
           response (including 101/400/426) indicates the server is reachable.
        2. ICE config endpoint at {skcomm_url}/api/v1/webrtc/ice-config — a 200
           response is parsed for ice_servers and active_peers.

        Returns:
            dict with keys:
                signaling_ok (bool): WebSocket endpoint is reachable.
                ice_servers_configured (bool): At least one ICE server found.
                active_peers (int): Number of active WebRTC peers (0 if unknown).
        """
        import httpx

        signaling_ok = False
        ice_servers_configured = False
        active_peers = 0

        ws_url = f"{self._skcomm_base}/webrtc/ws"
        try:
            resp = httpx.get(ws_url, timeout=_PING_TIMEOUT)
            # Any HTTP response means the signaling server is listening.
            # 101 = Switching Protocols, 400/426 = server up but requires upgrade.
            signaling_ok = resp.status_code in (101, 200, 400, 426)
        except Exception as exc:
            logger.debug("Watchdog: WebRTC signaling check error: %s", exc)

        ice_url = f"{self._skcomm_base}/api/v1/webrtc/ice-config"
        try:
            resp = httpx.get(ice_url, timeout=_PING_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                servers = data.get("ice_servers") or data.get("iceServers") or []
                ice_servers_configured = bool(servers)
                active_peers = int(data.get("active_peers") or data.get("activePeers") or 0)
        except Exception as exc:
            logger.debug("Watchdog: WebRTC ICE config check error: %s", exc)

        return {
            "signaling_ok": signaling_ok,
            "ice_servers_configured": ice_servers_configured,
            "active_peers": active_peers,
        }

    def health_summary(self) -> dict:
        """Return a full health summary across all transports.

        Returns:
            dict with keys:
                skcomm_ok (bool): SKComm HTTP health OK.
                transport_status (str): 'healthy' / 'degraded' / 'unreachable'.
                webrtc (dict): Result of check_webrtc().
                file_transport_available (bool): Always True (always present).
                uptime_seconds (float): Seconds since watchdog was created.
                consecutive_failures (int): Current failure streak count.
        """
        skcomm_ok = self.ping_skcomm()
        webrtc = self.check_webrtc()
        return {
            "skcomm_ok": skcomm_ok,
            "transport_status": self.transport_status,
            "webrtc": webrtc,
            "file_transport_available": True,
            "uptime_seconds": self.uptime_seconds,
            "consecutive_failures": self.consecutive_failures,
        }

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def uptime_seconds(self) -> float:
        """Seconds elapsed since this watchdog was created."""
        return (datetime.now(timezone.utc) - self._started_at).total_seconds()

    @property
    def transport_status(self) -> str:
        """Human-readable health label.

        Returns:
            'healthy'   -- zero consecutive failures.
            'degraded'  -- 1 to (threshold-1) consecutive failures.
            'unreachable' -- at or above failure_threshold.
        """
        if self.consecutive_failures == 0:
            return "healthy"
        if self.consecutive_failures < self._failure_threshold:
            return "degraded"
        return "unreachable"

    @property
    def is_healthy(self) -> bool:
        """True when consecutive_failures is zero."""
        return self.consecutive_failures == 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _trigger_reconnect(self) -> None:
        """Attempt to reconnect the transport by calling reconnect()."""
        logger.warning(
            "Watchdog: triggering transport.reconnect() after %d consecutive failures",
            self.consecutive_failures,
        )
        if self._transport is None:
            return
        reconnect = getattr(self._transport, "reconnect", None)
        if reconnect is None:
            logger.warning("Watchdog: transport has no reconnect() method -- skipping")
            return
        try:
            reconnect()
            logger.info("Watchdog: transport.reconnect() returned")
        except Exception as exc:
            logger.error("Watchdog: reconnect() raised: %s", exc)


# Alias for backwards compatibility and external tooling that imports ChatWatchdog.
ChatWatchdog = TransportWatchdog
