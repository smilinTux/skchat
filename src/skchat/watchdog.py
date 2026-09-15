"""Transport watchdog -- monitors SKComms health and triggers reconnect.

Pings the SKComms HTTP health endpoint on each check cycle.  Consecutive
failures at or above the configured threshold trigger transport.reconnect()
to attempt recovery.  The failure counter resets on the next successful ping.

Typical usage from the daemon main loop (every ~30s)::

    watchdog = TransportWatchdog(transport=skcomms)
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
#: Ceiling on the gap (in consecutive failures) between reconnect attempts. The
#: daemon checks roughly every 30s, so 120 is about one retry per hour once a
#: streak is long. Bounded so a permanently unreachable peer is retried forever
#: at a sane rate rather than hammered, and never abandoned.
_MAX_RECONNECT_GAP = 120


class TransportWatchdog:
    """Monitors SKComms transport health via HTTP ping.

    On each check() call, {skcomms_url}/health is polled with an
    HTTP GET (timeout=5s).  Consecutive failures at or above
    failure_threshold trigger transport.reconnect(), then again on a
    widening gap (threshold * 2**attempt, capped at _MAX_RECONNECT_GAP)
    for as long as the streak lasts.  A successful ping resets the streak
    and the backoff together.

    The gap matters: reconnect used to fire exactly once per streak and
    re-arm only on recovery, so a reconnect that did not fix the problem
    disabled all further reconnects.  Observed on noroc2027 2026-09-15 at
    consecutive=13954 against a threshold of 3, climbing since 2026-08-16
    with exactly one reconnect ever attempted.

    Args:
        transport: Object with an optional reconnect() method.
            Typically the SKComms instance used by the daemon.
        skcomms_url: Base URL of the SKComms HTTP API.
        failure_threshold: Number of consecutive failures before reconnect
            is triggered.  Defaults to 3.
    """

    def __init__(
        self,
        transport: object,
        skcomms_url: str = "http://127.0.0.1:9384",
        failure_threshold: int = _FAILURE_THRESHOLD,
    ) -> None:
        self._transport = transport
        self._skcomms_base = skcomms_url.rstrip("/")
        self._health_url = f"{self._skcomms_base}/health"
        self._failure_threshold = failure_threshold
        self.consecutive_failures: int = 0
        self.last_success_at: Optional[datetime] = None
        self.last_failure_at: Optional[datetime] = None
        self._reconnect_pending: bool = False
        #: Failure count at which the next reconnect fires. Re-armed with a
        #: widening gap after every attempt so a reconnect that does not fix
        #: the problem is retried instead of latching the watchdog off.
        self._next_reconnect_at: int = failure_threshold
        self._reconnect_attempts: int = 0
        self._started_at: datetime = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ping_skcomms(self) -> bool:
        """Ping {skcomms_url}/health with a 5-second timeout.

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
                        "Watchdog: SKComms healthy again (was %d consecutive failures)",
                        self.consecutive_failures,
                    )
                self.consecutive_failures = 0
                self.last_success_at = datetime.now(timezone.utc)
                self._reconnect_pending = False
                self._next_reconnect_at = self._failure_threshold
                self._reconnect_attempts = 0
                return True
            logger.debug("Watchdog: SKComms health returned HTTP %d", resp.status_code)
        except Exception as exc:
            logger.debug("Watchdog: SKComms health ping error: %s", exc)

        self.consecutive_failures += 1
        self.last_failure_at = datetime.now(timezone.utc)
        return False

    def check(self) -> bool:
        """Run one watchdog cycle.

        Calls ping_skcomms().  When consecutive_failures reaches
        failure_threshold, calls transport.reconnect() once per streak.

        Returns:
            bool: True if SKComms is healthy.
        """
        ok = self.ping_skcomms()
        if ok:
            return True

        logger.warning(
            "Watchdog: SKComms health check failed (consecutive=%d/%d)",
            self.consecutive_failures,
            self._failure_threshold,
        )
        if self.consecutive_failures >= self._next_reconnect_at:
            self._reconnect_pending = True
            self._reconnect_attempts += 1
            if self._reconnect_attempts > 1:
                # The previous reconnect did not restore health. Say so at ERROR:
                # a streak this long is an outage, not a blip, and the per-check
                # WARNING above is identical every time so it reads as noise.
                logger.error(
                    "Watchdog: SKComms still unreachable after %d reconnect attempt(s) "
                    "and %d consecutive failed checks. Escalating: this is an outage, "
                    "not a transient blip.",
                    self._reconnect_attempts - 1,
                    self.consecutive_failures,
                )
            self._trigger_reconnect()
            gap = min(self._failure_threshold * (2**self._reconnect_attempts), _MAX_RECONNECT_GAP)
            self._next_reconnect_at = self.consecutive_failures + gap
        return False

    def check_webrtc(self) -> dict:
        """Check WebRTC signaling connectivity.

        Probes two endpoints:
        1. WebSocket signaling endpoint at {skcomms_url}/webrtc/ws — any HTTP
           response (including 101/400/426) indicates the server is reachable.
        2. ICE config endpoint at {skcomms_url}/api/v1/webrtc/ice-config — a 200
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

        ws_url = f"{self._skcomms_base}/webrtc/ws"
        try:
            resp = httpx.get(ws_url, timeout=_PING_TIMEOUT)
            # Any HTTP response means the signaling server is listening.
            # 101 = Switching Protocols, 400/426 = server up but requires upgrade.
            signaling_ok = resp.status_code in (101, 200, 400, 426)
        except Exception as exc:
            logger.debug("Watchdog: WebRTC signaling check error: %s", exc)

        ice_url = f"{self._skcomms_base}/api/v1/webrtc/ice-config"
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
                skcomms_ok (bool): SKComms HTTP health OK.
                transport_status (str): 'healthy' / 'degraded' / 'unreachable'.
                webrtc (dict): Result of check_webrtc().
                file_transport_available (bool): Always True (always present).
                uptime_seconds (float): Seconds since watchdog was created.
                consecutive_failures (int): Current failure streak count.
        """
        skcomms_ok = self.ping_skcomms()
        webrtc = self.check_webrtc()
        return {
            "skcomms_ok": skcomms_ok,
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
