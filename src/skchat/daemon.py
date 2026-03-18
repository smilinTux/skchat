"""SKChat receive daemon — background polling for incoming messages.

This module provides a background service that continuously polls SKComm
transports for incoming chat messages and stores them in local history.

The daemon can be run as:
- A foreground process with `skchat daemon`
- A background systemd service
- A screen/tmux session
- Via the existing `skchat watch` command
"""

from __future__ import annotations

import json
import logging
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# SKComm is imported at module level so tests can patch skchat.daemon.SKComm.
try:
    from skcomm import SKComm  # type: ignore
except ImportError:  # pragma: no cover
    SKComm = None  # type: ignore

# Exponential backoff delays (seconds) for consecutive transport poll failures.
# Index 0 = 1st failure delay; last entry is the cap applied for all further failures.
_BACKOFF_DELAYS: tuple = (5, 10, 20, 40, 60)
_BACKOFF_ERROR_THRESHOLD: int = 5  # emit ERROR after this many consecutive failures


class DaemonShutdown(Exception):
    """Raised to trigger graceful daemon shutdown."""


class ChatDaemon:
    """Background daemon for receiving chat messages.

    Continuously polls SKComm transports and stores incoming messages
    in the local chat history.

    Args:
        interval: Poll interval in seconds (default: 5)
        log_file: Optional path to log file for daemon output
        quiet: If True, suppress console output
    """

    def __init__(
        self,
        interval: float = 5.0,
        log_file: Optional[Path] = None,
        quiet: bool = False,
    ) -> None:
        self.interval = interval
        self.log_file = log_file
        self.quiet = quiet
        self.running = False
        self.total_received = 0
        self.last_poll_time: Optional[datetime] = None
        self.poll_count = 0
        self._webrtc_active = False
        self._transport_ok: bool = False
        self._consecutive_failures: int = 0
        self.total_sent: int = 0
        self.start_time: Optional[datetime] = None
        self.last_heartbeat_at: Optional[datetime] = None
        self.advocacy_responses: int = 0
        self._outbox_messenger: Optional[object] = None
        self._skcomm: Optional[object] = None  # set in start(), used for reconnect

        if log_file:
            logging.basicConfig(
                filename=str(log_file),
                level=logging.INFO,
                format="%(asctime)s [%(levelname)s] %(message)s",
            )

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame) -> None:
        """Handle shutdown signals gracefully.

        Args:
            signum: Signal number
            frame: Current stack frame
        """
        logger.info(f"Received signal {signum}, shutting down gracefully...")
        self.running = False
        raise DaemonShutdown()

    def _log(self, message: str, level: str = "info") -> None:
        """Log a message to file and optionally console.

        Args:
            message: Message to log
            level: Log level (info, warning, error)
        """
        log_func = getattr(logger, level, logger.info)
        log_func(message)

        if not self.quiet:
            print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] {message}")

    def start(self) -> None:
        """Start the daemon polling loop.

        Continuously polls for messages until stopped via signal or error.
        Also runs the ephemeral message reaper and drains the outbox queue
        on each cycle for full E2E P2P messaging.

        Raises:
            ImportError: If required dependencies are not available.
            Exception: If transport cannot be initialized.
        """
        try:
            from .transport import ChatTransport
            from .history import ChatHistory
            from .identity_bridge import get_sovereign_identity
        except ImportError as exc:
            self._log(f"Failed to import required modules: {exc}", "error")
            raise

        try:
            if SKComm is None:
                raise ImportError("skcomm package not installed")
            skcomm = SKComm.from_config()
            self._skcomm = skcomm
        except Exception as exc:
            self._log(f"Failed to initialize SKComm: {exc}", "error")
            self._log("Make sure SKComm is configured: skcomm init", "error")
            raise

        try:
            history = ChatHistory.from_config()
        except Exception as exc:
            logger.warning("ChatHistory.from_config() failed, trying in-memory fallback: %s", exc)
            try:
                from skmemory import MemoryStore
                history = ChatHistory(store=MemoryStore())
            except Exception as fallback_exc:
                logger.error("In-memory fallback also failed (%s); re-raising original error", fallback_exc)
                raise exc

        identity = get_sovereign_identity()

        try:
            transport = ChatTransport.from_config(
                skcomm=skcomm,
                history=history,
                identity=identity,
            )
        except Exception as exc:
            self._log(f"Failed to initialize transport: {exc}", "error")
            raise

        # Mark running early so the poll loop can start immediately, before slow
        # subsystem init (SQLite, imports) completes.  All subsystem references
        # are None-checked in the loop so None is always safe until the bg thread
        # populates them.
        self.start_time = datetime.now(timezone.utc)
        self.running = True

        reaper = None
        presence = None
        queue = None
        bridge = None
        watchdog = None
        engine = None
        plugin_registry = None

        def _init_subsystems_bg() -> None:
            nonlocal reaper, presence, queue, bridge, watchdog, engine, plugin_registry
            reaper = self._init_reaper(history)
            presence = self._init_presence(identity)
            queue = self._init_queue(skcomm, identity)
            bridge = self._init_memory_bridge(history)
            self._init_webrtc(skcomm, identity)
            watchdog = self._init_watchdog(skcomm)
            try:
                from skchat.advocacy import AdvocacyEngine
                engine = AdvocacyEngine(identity=identity)
            except Exception as exc:
                self._log(f"AdvocacyEngine init skipped: {exc}", "warning")
            try:
                from .plugins import PluginRegistry
                pr = PluginRegistry()
                pr.discover()
                plugin_registry = pr
            except Exception as exc:
                self._log(f"PluginRegistry init skipped: {exc}", "warning")
            subsystems = [
                k for k, v in [
                    ("reaper", reaper), ("presence", presence), ("queue", queue),
                    ("watchdog", watchdog), ("memory-bridge", bridge),
                ]
                if v
            ]
            if self._webrtc_active:
                subsystems.append("webrtc")
            self._log(f"Subsystems ready: {', '.join(subsystems) or 'none'}")

        threading.Thread(target=_init_subsystems_bg, daemon=True, name="skchat-init").start()

        self._log(f"SKChat daemon starting (identity: {identity})")
        self._log(f"Polling every {self.interval}s, Ctrl+C to stop")

        self._start_health_server()
        reap_counter = 0
        presence_counter = 0
        memory_bridge_counter = 0
        watchdog_counter = 0

        try:
            while self.running:
                self.poll_count += 1
                self.last_poll_time = datetime.now(timezone.utc)

                # --- Poll for incoming messages ---
                try:
                    messages = transport.poll_inbox()

                    # Transport succeeded — reset backoff counter
                    if self._consecutive_failures > 0:
                        logger.info(
                            "Transport recovered after %d consecutive failure(s)",
                            self._consecutive_failures,
                        )
                        self._consecutive_failures = 0
                    self._transport_ok = True

                    if messages:
                        self.total_received += len(messages)
                        self._log(f"Received {len(messages)} message(s) (total: {self.total_received})")

                        for msg in messages:
                            sender_short = msg.sender.split("@")[0].replace("capauth:", "")
                            preview = msg.content[:60] + ("..." if len(msg.content) > 60 else "")
                            self._log(f"  [{sender_short}] {preview}")
                            try:
                                import subprocess
                                subprocess.run(
                                    ["notify-send", "SKChat", f"[{sender_short}] {preview}"],
                                    capture_output=True,
                                )
                            except Exception as exc:
                                logger.warning("notify-send failed: %s", exc)
                            if engine:
                                try:
                                    reply = engine.process_message(msg)
                                    if reply:
                                        transport.send_and_store(msg.sender, reply)
                                        self.advocacy_responses += 1
                                except Exception as exc:
                                    self._log(f"Advocacy error: {exc}", "warning")
                            if plugin_registry:
                                for plugin in plugin_registry.get_plugins():
                                    if plugin.should_handle(msg):
                                        try:
                                            plugin_reply = plugin.handle(msg)
                                            if plugin_reply:
                                                transport.send_and_store(msg.sender, plugin_reply)
                                        except Exception as exc:
                                            self._log(f"Plugin '{plugin.name}' error: {exc}", "warning")
                    else:
                        if self.poll_count % 12 == 0:
                            self._log(f"No new messages (polls: {self.poll_count}, uptime: {self._uptime()})")

                except Exception as exc:
                    self._consecutive_failures += 1
                    self._transport_ok = False
                    delay = _BACKOFF_DELAYS[
                        min(self._consecutive_failures - 1, len(_BACKOFF_DELAYS) - 1)
                    ]
                    self._log(
                        f"Transport poll failed (attempt {self._consecutive_failures},"
                        f" retrying in {delay}s): {exc}",
                        "warning",
                    )
                    if self._consecutive_failures >= _BACKOFF_ERROR_THRESHOLD:
                        logger.error(
                            "Transport has failed %d consecutive time(s);"
                            " check SKComm connectivity",
                            self._consecutive_failures,
                        )
                    # Attempt transport reconnect on the 2nd consecutive failure
                    # so recovery is faster than waiting for the watchdog cycle.
                    if self._consecutive_failures == 2 and self._skcomm is not None:
                        reconnect_fn = getattr(self._skcomm, 'reconnect', None)
                        if reconnect_fn:
                            try:
                                logger.info(
                                    "Transport: attempting reconnect after %d failures",
                                    self._consecutive_failures,
                                )
                                reconnect_fn()
                            except Exception as rc_exc:
                                logger.warning("Transport reconnect failed: %s", rc_exc)
                    # Cap the sleep at the configured poll interval so
                    # low-interval daemons (e.g. tests with interval=0.1s)
                    # can still cycle promptly. For the default interval=5s
                    # the first backoff delay is also 5s, so behaviour is
                    # unchanged in production.
                    time.sleep(min(delay, self.interval))
                    continue

                # --- Reap expired ephemeral messages (every 6 cycles ~30s) ---
                reap_counter += 1
                if reaper and reap_counter >= 6:
                    reap_counter = 0
                    try:
                        result = reaper.sweep(create_tombstones=True)
                        if result.expired > 0:
                            self._log(f"Reaper: {result.expired} expired, {result.active_ephemeral} still active")
                    except Exception as exc:
                        self._log(f"Reaper error: {exc}", "warning")

                # --- Process outbox queue each cycle (backoff inside process_pending) ---
                if queue:
                    try:
                        delivered, failed = queue.process_pending(self._outbox_messenger)
                        if delivered > 0 or failed > 0:
                            self._log(f"Outbox: {delivered} delivered, {failed} retried/failed")
                        self.total_sent += delivered
                    except Exception as exc:
                        self._log(f"Outbox process error: {exc}", "warning")

                # --- Broadcast presence (every 12 cycles ~60s) ---
                presence_counter += 1
                if presence and presence_counter >= 12:
                    presence_counter = 0
                    try:
                        self._broadcast_presence(skcomm, identity, presence)
                        self.last_heartbeat_at = datetime.now(timezone.utc)
                    except Exception as exc:
                        self._log(f"Presence broadcast error: {exc}", "warning")

                # --- Auto-capture active threads to skcapstone (every 720 cycles ~1h) ---
                memory_bridge_counter += 1
                if bridge and memory_bridge_counter >= 720:
                    memory_bridge_counter = 0
                    try:
                        results = bridge.auto_capture()
                        if results:
                            self._log(
                                f"MemoryBridge: captured {len(results)} thread(s) to skcapstone"
                            )
                    except Exception as exc:
                        self._log(f"MemoryBridge auto-capture error: {exc}", "warning")

                # --- Watchdog health check + stats file write (every 6 cycles ~30s) ---
                watchdog_counter += 1
                if watchdog_counter >= 6:
                    watchdog_counter = 0
                    if watchdog:
                        try:
                            watchdog.check()
                        except Exception as exc:
                            self._log(f"Watchdog error: {exc}", "warning")
                    try:
                        self._write_daemon_stats(watchdog, presence, skcomm)
                    except Exception as exc:
                        logger.warning("Daemon stats write error: %s", exc)

                time.sleep(self.interval)

        except DaemonShutdown:
            pass
        except KeyboardInterrupt:
            pass
        finally:
            # Send offline presence on shutdown
            if presence:
                try:
                    self._broadcast_presence(skcomm, identity, presence, going_offline=True)
                except Exception as exc:
                    logger.warning("Failed to send offline presence on shutdown: %s", exc)
            self._log(f"Daemon stopped. Received {self.total_received} message(s) total.")

    def _init_reaper(self, history: object) -> object:
        """Initialize the ephemeral message reaper.

        Args:
            history: ChatHistory instance.

        Returns:
            MessageReaper or None if initialization fails.
        """
        try:
            from .ephemeral import MessageReaper
            return MessageReaper(store=history._store)
        except Exception as exc:
            self._log(f"Reaper init skipped: {exc}", "warning")
            return None

    def _init_presence(self, identity: str) -> object:
        """Initialize the presence tracker.

        Args:
            identity: Local identity URI.

        Returns:
            PresenceTracker or None if initialization fails.
        """
        try:
            from .presence import PresenceTracker
            return PresenceTracker()
        except Exception as exc:
            self._log(f"Presence init skipped: {exc}", "warning")
            return None

    def _init_queue(self, skcomm: object, identity: str) -> object:
        """Initialize the outbox message queue for retry delivery.

        Also initialises the AgentMessenger stored on self._outbox_messenger
        so deliver_pending() has a send channel without re-creating it each cycle.

        Args:
            skcomm: SKComm instance.
            identity: Local CapAuth identity URI.

        Returns:
            OutboxQueue or None if initialization fails.
        """
        try:
            from .outbox import OutboxQueue
            from .agent_comm import AgentMessenger

            queue = OutboxQueue()
            try:
                self._outbox_messenger = AgentMessenger.from_identity(
                    identity, skcomm=skcomm
                )
            except Exception as exc:
                self._log(f"Outbox messenger init skipped: {exc}", "warning")
            return queue
        except Exception as exc:
            self._log(f"Queue init skipped: {exc}", "warning")
            return None

    def _init_webrtc(self, skcomm: object, identity: str) -> None:
        """Wire the WebRTC transport to the chat daemon if available.

        Finds the WebRTC transport in the SKComm router and starts it
        if it hasn't been started yet. Stores incoming WEBRTC_SIGNAL
        envelopes as chat messages in the history for call management.

        Args:
            skcomm: Initialized SKComm instance.
            identity: Local identity URI (for call routing).
        """
        try:
            webrtc_transport = None
            for t in skcomm.router.transports:
                if t.name == "webrtc":
                    webrtc_transport = t
                    break

            if webrtc_transport is None:
                return

            # Start the transport if not already running
            if hasattr(webrtc_transport, "start") and not webrtc_transport._running:
                webrtc_transport.start()

            self._webrtc_active = True
            self._log("WebRTC transport wired (signaling connected on next poll)")

        except Exception as exc:
            self._log(f"WebRTC init skipped: {exc}", "warning")


    def _init_memory_bridge(self, history: object) -> object:
        """Initialize the MemoryBridge for hourly auto-capture to skcapstone.

        Args:
            history: ChatHistory instance.

        Returns:
            MemoryBridge or None if initialization fails.
        """
        try:
            from .memory_bridge import MemoryBridge
            return MemoryBridge(history=history)
        except Exception as exc:
            self._log(f"MemoryBridge init skipped: {exc}", "warning")
            return None

    def _broadcast_presence(
        self,
        skcomm: object,
        identity: str,
        tracker: object,
        going_offline: bool = False,
    ) -> None:
        """Broadcast presence state over SKComm.

        Args:
            skcomm: SKComm instance.
            identity: Local identity URI.
            tracker: PresenceTracker instance.
            going_offline: If True, send offline status.
        """
        from .presence import PresenceIndicator, PresenceState

        state = PresenceState.OFFLINE if going_offline else PresenceState.ONLINE
        indicator = PresenceIndicator(
            identity_uri=identity,
            state=state,
        )
        tracker.update(indicator)

        # Persist own presence to the file-backed cache so CLI / MCP tools
        # (skchat who, who_is_online) can read it without being in-process.
        try:
            from .presence import PresenceCache
            PresenceCache().record(identity, state, indicator.timestamp)
        except Exception as exc:
            logger.debug("PresenceCache.record failed: %s", exc)

        try:
            payload = indicator.model_dump_json()
            from skcomm.models import MessageType
            skcomm.send(
                recipient="*",
                message=payload,
                message_type=MessageType.HEARTBEAT,
            )
        except Exception as exc:
            logger.warning("Presence broadcast failed: %s", exc)

    def _uptime(self) -> str:
        """Calculate daemon uptime.

        Returns:
            str: Human-readable uptime (e.g., "5m 30s")
        """
        if self.start_time:
            uptime_seconds = int((datetime.now(timezone.utc) - self.start_time).total_seconds())
        elif self.last_poll_time:
            uptime_seconds = int(self.poll_count * self.interval)
        else:
            return "0s"
        
        if uptime_seconds < 60:
            return f"{uptime_seconds}s"
        elif uptime_seconds < 3600:
            minutes = uptime_seconds // 60
            seconds = uptime_seconds % 60
            return f"{minutes}m {seconds}s"
        else:
            hours = uptime_seconds // 3600
            minutes = (uptime_seconds % 3600) // 60
            return f"{hours}h {minutes}m"

    def _start_health_server(self, port: int = 9385) -> None:
        """Start a tiny HTTP healthcheck server in a daemon thread.

        Serves GET /health → JSON with live daemon metrics.

        Args:
            port: TCP port to listen on (default: 9385).
        """
        daemon_ref = self

        class _HealthHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                uptime_s = 0
                if daemon_ref.start_time:
                    uptime_s = int(
                        (datetime.now(timezone.utc) - daemon_ref.start_time).total_seconds()
                    )

                if self.path == "/metrics":
                    identity = getattr(daemon_ref, "_identity", "unknown")
                    transport_ok = 1 if daemon_ref._transport_ok else 0
                    advocacy_responses = getattr(daemon_ref, "advocacy_responses", 0)
                    online_peers = getattr(daemon_ref, "_online_peer_count", 0)
                    body = (
                        f'# HELP skchat_uptime_seconds Daemon uptime in seconds\n'
                        f'# TYPE skchat_uptime_seconds gauge\n'
                        f'skchat_uptime_seconds{{identity="{identity}"}} {uptime_s}\n'
                        f'# HELP skchat_messages_received_total Total messages received\n'
                        f'# TYPE skchat_messages_received_total counter\n'
                        f'skchat_messages_received_total{{identity="{identity}"}} {daemon_ref.total_received}\n'
                        f'# HELP skchat_messages_sent_total Total messages sent\n'
                        f'# TYPE skchat_messages_sent_total counter\n'
                        f'skchat_messages_sent_total{{identity="{identity}"}} {getattr(daemon_ref, "total_sent", 0)}\n'
                        f'# HELP skchat_advocacy_responses_total Total advocacy auto-responses\n'
                        f'# TYPE skchat_advocacy_responses_total counter\n'
                        f'skchat_advocacy_responses_total{{identity="{identity}"}} {advocacy_responses}\n'
                        f'# HELP skchat_peers_online Number of online peers\n'
                        f'# TYPE skchat_peers_online gauge\n'
                        f'skchat_peers_online{{identity="{identity}"}} {online_peers}\n'
                        f'# HELP skchat_transport_ok Transport health (1=ok, 0=down)\n'
                        f'# TYPE skchat_transport_ok gauge\n'
                        f'skchat_transport_ok{{identity="{identity}"}} {transport_ok}\n'
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; version=0.0.4")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if self.path != "/health":
                    self.send_response(404)
                    self.end_headers()
                    return

                last_poll_at = (
                    daemon_ref.last_poll_time.isoformat()
                    if daemon_ref.last_poll_time
                    else None
                )

                body = json.dumps({
                    "status": "ok" if daemon_ref.running else "stopping",
                    "uptime_s": uptime_s,
                    "messages_received": daemon_ref.total_received,
                    "last_poll_at": last_poll_at,
                    "transport_ok": daemon_ref._transport_ok,
                }).encode()

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args) -> None:  # noqa: N802
                pass  # suppress access log noise

        try:
            server = HTTPServer(("127.0.0.1", port), _HealthHandler)
        except OSError as exc:
            self._log(
                f"Health server could not bind to port {port}: {exc} (continuing without health endpoint)",
                "warning",
            )
            return
        thread = threading.Thread(
            target=server.serve_forever,
            daemon=True,
            name="skchat-health",
        )
        thread.start()
        self._log(f"Health endpoint listening on http://127.0.0.1:{port}/health")

    def _init_watchdog(self, skcomm: object) -> object:
        """Initialize the transport watchdog.

        Args:
            skcomm: SKComm instance to monitor and reconnect.

        Returns:
            TransportWatchdog or None if initialization fails.
        """
        try:
            from .watchdog import TransportWatchdog
            return TransportWatchdog(transport=skcomm)
        except Exception as exc:
            self._log(f"Watchdog init skipped: {exc}", "warning")
            return None

    def _write_daemon_stats(
        self,
        watchdog: object,
        presence: object,
        skcomm: object,
    ) -> None:
        """Write daemon runtime stats to the stats JSON file.

        Stats are consumed by daemon_status() and the MCP daemon_status tool
        to expose uptime, message counts, and transport health across processes.

        Args:
            watchdog: TransportWatchdog instance (or None).
            presence: PresenceTracker instance (or None).
            skcomm: SKComm instance (or None).
        """
        stats_path = _DAEMON_STATS_FILE.expanduser()

        uptime_seconds = 0
        if self.start_time:
            uptime_seconds = int(
                (datetime.now(timezone.utc) - self.start_time).total_seconds()
            )

        transport_status = "unknown"
        if watchdog:
            transport_status = watchdog.transport_status

        online_peer_count = 0
        if presence:
            try:
                online_peer_count = len(presence.who_is_online())
            except Exception as exc:
                logger.warning("presence.who_is_online() failed: %s", exc)

        webrtc_signaling_ok = False
        if skcomm:
            try:
                for t in skcomm.router.transports:
                    if t.name == "webrtc":
                        webrtc_signaling_ok = bool(
                            getattr(t, "_signaling_connected", False)
                        )
                        break
            except Exception as exc:
                logger.warning("Failed to read WebRTC signaling state: %s", exc)

        stats = {
            "uptime_seconds": uptime_seconds,
            "messages_sent": self.total_sent,
            "messages_received": self.total_received,
            "transport_status": transport_status,
            "webrtc_signaling_ok": webrtc_signaling_ok,
            "last_heartbeat_at": (
                self.last_heartbeat_at.isoformat()
                if self.last_heartbeat_at
                else None
            ),
            "online_peer_count": online_peer_count,
            "advocacy_responses": self.advocacy_responses,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        stats_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = stats_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(stats))
        tmp.rename(stats_path)

    @staticmethod
    def from_config(config_path: Optional[Path] = None) -> "ChatDaemon":
        """Create a daemon from environment variables and optional config file.

        When called without arguments, only environment variables are used
        (SKCHAT_DAEMON_INTERVAL, SKCHAT_DAEMON_LOG, SKCHAT_DAEMON_QUIET).
        Pass an explicit *config_path* to also read settings from a YAML file;
        environment variables always take priority over file values.

        Args:
            config_path: Optional path to a YAML config file.  When None,
                no config file is read.

        Returns:
            ChatDaemon: Configured daemon instance.
        """
        import os

        # Start with built-in defaults
        interval = 5.0
        log_file: Optional[str] = None
        quiet = False

        # Apply YAML config file values first (lowest priority)
        if config_path is not None and config_path.exists():
            try:
                import yaml
                with open(config_path) as f:
                    cfg = yaml.safe_load(f) or {}

                daemon_cfg = cfg.get("daemon", {})
                if "poll_interval" in daemon_cfg:
                    interval = float(daemon_cfg["poll_interval"])
                if "log_file" in daemon_cfg:
                    log_file = daemon_cfg["log_file"]
                if "quiet" in daemon_cfg:
                    quiet = bool(daemon_cfg["quiet"])
            except (ImportError, OSError) as exc:
                logger.warning("Failed to read config %s: %s", config_path, exc)

        # Environment variables always override config file values
        env_interval = os.environ.get("SKCHAT_DAEMON_INTERVAL")
        if env_interval:
            interval = float(env_interval)
        env_log = os.environ.get("SKCHAT_DAEMON_LOG")
        if env_log:
            log_file = env_log
        env_quiet = os.environ.get("SKCHAT_DAEMON_QUIET")
        if env_quiet:
            quiet = env_quiet.lower() in ("1", "true", "yes")

        log_path = Path(log_file).expanduser() if log_file else None

        return ChatDaemon(
            interval=interval,
            log_file=log_path,
            quiet=quiet,
        )


DAEMON_PID_FILE = Path("~/.skchat/daemon.pid")
DAEMON_LOG_FILE = Path("~/.skchat/daemon.log")
DAEMON_STATS_FILE = Path("~/.skchat/daemon_stats.json")
_DAEMON_STATS_FILE = DAEMON_STATS_FILE  # internal alias used by _write_daemon_stats


def _pid_file() -> Path:
    """Return the expanded path to the daemon PID file."""
    return DAEMON_PID_FILE.expanduser()


def _read_pid() -> Optional[int]:
    """Read the daemon PID from the PID file.

    Returns:
        int: The PID if the file exists and is valid, else None.
    """
    pid_path = _pid_file()
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return None


def _write_pid(pid: int) -> None:
    """Write the daemon PID to the PID file.

    Args:
        pid: Process ID to write.
    """
    pid_path = _pid_file()
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(pid))


def _remove_pid() -> None:
    """Remove the daemon PID file if it exists."""
    pid_path = _pid_file()
    pid_path.unlink(missing_ok=True)


def is_running() -> bool:
    """Check if the daemon process is currently running.

    Reads the PID file and checks if the process exists.

    Returns:
        bool: True if daemon is alive, False otherwise.
    """
    import os

    pid = _read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def daemon_status() -> dict:
    """Get the current daemon status including runtime statistics.

    Reads the PID file to determine if the daemon is running, then merges
    in the stats written by ChatDaemon._write_daemon_stats (uptime,
    message counts, transport health, heartbeat time, online peers).

    Returns:
        dict with keys: running, pid, pid_file, log_file, plus
        uptime_seconds, messages_sent, messages_received,
        transport_status, webrtc_signaling_ok, last_heartbeat_at,
        online_peer_count, updated_at (when daemon was last running).
    """
    pid = _read_pid()
    running = is_running()
    if not running and pid is not None:
        _remove_pid()
        pid = None

    status: dict = {
        "running": running,
        "pid": pid,
        "pid_file": str(_pid_file()),
        "log_file": str(DAEMON_LOG_FILE.expanduser()),
    }

    stats_file = DAEMON_STATS_FILE.expanduser()
    if stats_file.exists():
        try:
            stats = json.loads(stats_file.read_text())
            status.update(stats)
        except Exception as exc:
            logger.debug("Failed to read daemon stats file: %s", exc)

    return status


def start_daemon(
    interval: float = 5.0,
    log_file: Optional[str] = None,
    quiet: bool = False,
    background: bool = True,
) -> int:
    """Start the daemon as a background process.

    Forks a subprocess that writes its PID to ~/.skchat/daemon.pid
    and runs the polling loop. Returns immediately if background=True.

    Args:
        interval: Poll interval in seconds.
        log_file: Optional log file path.
        quiet: Suppress console output in daemon process.
        background: If True, daemonize (default). If False, run in foreground.

    Returns:
        int: PID of the daemon process.

    Raises:
        RuntimeError: If daemon is already running.
    """
    import os
    import subprocess

    if is_running():
        pid = _read_pid()
        raise RuntimeError(f"Daemon already running (PID {pid})")

    if not background:
        log_path = Path(log_file).expanduser() if log_file else None
        d = ChatDaemon(interval=interval, log_file=log_path, quiet=quiet)
        _write_pid(os.getpid())
        try:
            d.start()
        finally:
            _remove_pid()
        return os.getpid()

    # Resolve log file
    log_path = Path(log_file).expanduser() if log_file else DAEMON_LOG_FILE.expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "skchat._daemon_entry",
        "--interval", str(interval),
        "--log-file", str(log_path),
    ]
    if quiet:
        cmd.append("--quiet")

    with open(log_path, "a") as logf:
        proc = subprocess.Popen(
            cmd,
            stdout=logf,
            stderr=logf,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    _write_pid(proc.pid)
    logger.info("Started skchat daemon with PID %d", proc.pid)
    return proc.pid


def stop_daemon() -> Optional[int]:
    """Stop the running daemon by sending SIGTERM.

    Returns:
        int: The PID that was stopped, or None if nothing was running.
    """
    import os
    import time

    pid = _read_pid()
    if pid is None or not is_running():
        _remove_pid()
        return None

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _remove_pid()
        return None

    # Wait up to 5 seconds for it to exit
    for _ in range(50):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break

    _remove_pid()
    logger.info("Stopped skchat daemon (PID %d)", pid)
    return pid


def run_daemon(
    interval: float = 5.0,
    log_file: Optional[str] = None,
    quiet: bool = False,
) -> None:
    """Run the chat daemon in the foreground (blocking).

    Writes PID to ~/.skchat/daemon.pid on start, removes it on exit.

    Args:
        interval: Poll interval in seconds
        log_file: Optional path to log file
        quiet: If True, suppress console output

    Examples:
        >>> run_daemon(interval=10, log_file="~/.skchat/daemon.log")
    """
    import os

    log_path = Path(log_file).expanduser() if log_file else None
    daemon = ChatDaemon(interval=interval, log_file=log_path, quiet=quiet)

    _write_pid(os.getpid())
    try:
        daemon.start()
    except Exception as exc:
        logger.error(f"Daemon failed to start: {exc}")
        sys.exit(1)
    finally:
        _remove_pid()
