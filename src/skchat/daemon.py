"""SKChat receive daemon — background polling for incoming messages.

This module provides a background service that continuously polls SKComms
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

# SKComms is imported at module level so tests can patch skchat.daemon.SKComms.
try:
    from skcomms import SKComms  # type: ignore
except ImportError:  # pragma: no cover
    SKComms = None  # type: ignore

# Exponential backoff delays (seconds) for consecutive transport poll failures.
# Index 0 = 1st failure delay; last entry is the cap applied for all further failures.
_BACKOFF_DELAYS: tuple = (5, 10, 20, 40, 60)
_BACKOFF_ERROR_THRESHOLD: int = 5  # emit ERROR after this many consecutive failures


# Environment variable holding the WebRTC/coturn HMAC shared secret.
# When absent, relayed (TURN) calls can't authenticate — we warn but never
# fail: STUN-only / LAN / local fallback still works.
_TURN_SECRET_ENV: str = "SKCOMMS_TURN_SECRET"


def webrtc_signaling_health(
    *,
    webrtc_active: bool,
    signaling_connected: bool,
) -> str:
    """Classify WebRTC signaling state into ``ok`` / ``degraded`` / ``down``.

    Pure function — no I/O — so it is trivially testable without live
    signaling/TURN/STUN servers.

    Semantics:
        - ``down``      — the WebRTC transport isn't wired at all; calls are
          impossible (a stale ``signaling_connected`` flag doesn't change this).
        - ``degraded``  — transport is wired (LAN / local fallback usable) but
          the signaling server is unreachable; relayed calls won't connect.
        - ``ok``        — transport wired and signaling connected.

    Args:
        webrtc_active: Whether the WebRTC transport was successfully wired.
        signaling_connected: Whether the signaling server reports connected.

    Returns:
        str: One of ``"ok"``, ``"degraded"``, ``"down"``.
    """
    if not webrtc_active:
        return "down"
    return "ok" if signaling_connected else "degraded"


def turn_secret_present(env: Optional[dict] = None) -> bool:
    """Return True when a non-blank TURN shared secret is configured.

    Args:
        env: Environment mapping to inspect (defaults to ``os.environ``).

    Returns:
        bool: True when ``SKCOMMS_TURN_SECRET`` is set and non-blank.
    """
    import os

    source = env if env is not None else os.environ
    return bool((source.get(_TURN_SECRET_ENV) or "").strip())


def webrtc_turn_warning(env: Optional[dict] = None) -> Optional[str]:
    """Return a clear warning string when the TURN secret is missing, else None.

    Non-fatal by design: the caller logs this and continues. Without the
    secret, relayed (coturn) calls can't authenticate, so calls fall back to
    STUN/LAN only.

    Args:
        env: Environment mapping to inspect (defaults to ``os.environ``).

    Returns:
        Optional[str]: Warning message, or ``None`` when the secret is present.
    """
    if turn_secret_present(env):
        return None
    return (
        f"{_TURN_SECRET_ENV} not set — relayed (TURN/coturn) calls cannot "
        "authenticate; falling back to STUN/LAN only"
    )


class DaemonShutdown(Exception):
    """Raised to trigger graceful daemon shutdown."""


class ChatDaemon:
    """Background daemon for receiving chat messages.

    Continuously polls SKComms transports and stores incoming messages
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
        self._skcomms: Optional[object] = None  # set in start(), used for reconnect
        # Persistent receive-side file-transfer plumbing (set by _init_attachments).
        # _file_service stores incoming FILE_* chunks; _attachment_service.on_complete
        # is bound to it so a completed inbound transfer posts a chat message.
        self._file_service: Optional[object] = None
        self._attachment_service: Optional[object] = None

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
            from .history import ChatHistory
            from .identity_bridge import get_sovereign_identity
            from .transport import ChatTransport
        except ImportError as exc:
            self._log(f"Failed to import required modules: {exc}", "error")
            raise

        try:
            if SKComms is None:
                raise ImportError("skcomms package not installed")
            skcomms = SKComms.from_config()
            self._skcomms = skcomms
        except Exception as exc:
            logger.warning("daemon.py: %s", exc)
            self._log(f"Failed to initialize SKComms: {exc}", "error")
            self._log("Make sure SKComms is configured: skcomms init", "error")
            raise

        try:
            history = ChatHistory.from_config()
        except Exception as exc:
            logger.warning("ChatHistory.from_config() failed, trying in-memory fallback: %s", exc)
            try:
                from skmemory import MemoryStore

                history = ChatHistory(store=MemoryStore())
            except Exception as fallback_exc:
                logger.error(
                    "In-memory fallback also failed (%s); re-raising original error", fallback_exc
                )
                raise exc

        identity = get_sovereign_identity()

        try:
            transport = ChatTransport.from_config(
                skcomms=skcomms,
                history=history,
                identity=identity,
            )
        except Exception as exc:
            logger.warning("daemon.py: %s", exc)
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
            queue = self._init_queue(skcomms, identity)
            bridge = self._init_memory_bridge(history)
            self._init_webrtc(skcomms, identity)
            self._init_attachments(history=history, identity=identity, skcomms=skcomms)
            watchdog = self._init_watchdog(skcomms)
            try:
                from skchat.advocacy import AdvocacyEngine

                engine = AdvocacyEngine(identity=identity)
            except Exception as exc:
                logger.warning("daemon.py: %s", exc)
                self._log(f"AdvocacyEngine init skipped: {exc}", "warning")
            try:
                from .plugins import PluginRegistry

                pr = PluginRegistry()
                pr.discover()
                plugin_registry = pr
            except Exception as exc:
                logger.warning("daemon.py: %s", exc)
                self._log(f"PluginRegistry init skipped: {exc}", "warning")
            subsystems = [
                k
                for k, v in [
                    ("reaper", reaper),
                    ("presence", presence),
                    ("queue", queue),
                    ("watchdog", watchdog),
                    ("memory-bridge", bridge),
                ]
                if v
            ]
            if self._webrtc_active:
                subsystems.append("webrtc")
            if self._attachment_service is not None:
                subsystems.append("attachments")
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
                        self._log(
                            f"Received {len(messages)} message(s) (total: {self.total_received})"
                        )

                        for msg in messages:
                            # Route file-transfer envelopes to the receive-side
                            # FileTransferService (a completed transfer fires the
                            # bound on_complete, posting an inbound message). Skip
                            # normal advocacy/plugin/notify handling for them.
                            if self._route_file_message(msg):
                                continue
                            sender_short = msg.sender.split("@")[0].replace("capauth:", "")
                            preview = msg.content[:60] + ("..." if len(msg.content) > 60 else "")
                            self._log(f"  [{sender_short}] {preview}")
                            try:
                                import subprocess

                                from .notifications import desktop_notifications_enabled

                                if desktop_notifications_enabled():
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
                                    logger.warning("daemon.py: %s", exc)
                                    self._log(f"Advocacy error: {exc}", "warning")
                            if plugin_registry:
                                for plugin in plugin_registry.get_plugins():
                                    if plugin.should_handle(msg):
                                        try:
                                            plugin_reply = plugin.handle(msg)
                                            if plugin_reply:
                                                transport.send_and_store(msg.sender, plugin_reply)
                                        except Exception as exc:
                                            logger.warning("daemon.py: %s", exc)
                                            self._log(
                                                f"Plugin '{plugin.name}' error: {exc}", "warning"
                                            )
                    else:
                        if self.poll_count % 12 == 0:
                            self._log(
                                f"No new messages (polls: {self.poll_count}, uptime: {self._uptime()})"
                            )

                except Exception as exc:
                    logger.warning("daemon.py: %s", exc)
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
                            " check SKComms connectivity",
                            self._consecutive_failures,
                        )
                    # Attempt transport reconnect on the 2nd consecutive failure
                    # so recovery is faster than waiting for the watchdog cycle.
                    if self._consecutive_failures == 2 and self._skcomms is not None:
                        reconnect_fn = getattr(self._skcomms, "reconnect", None)
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
                            self._log(
                                f"Reaper: {result.expired} expired, {result.active_ephemeral} still active"
                            )
                    except Exception as exc:
                        logger.warning("daemon.py: %s", exc)
                        self._log(f"Reaper error: {exc}", "warning")

                # --- Process outbox queue each cycle (backoff inside process_pending) ---
                if queue:
                    try:
                        delivered, failed = queue.process_pending(self._outbox_messenger)
                        if delivered > 0 or failed > 0:
                            self._log(f"Outbox: {delivered} delivered, {failed} retried/failed")
                        self.total_sent += delivered
                    except Exception as exc:
                        logger.warning("daemon.py: %s", exc)
                        self._log(f"Outbox process error: {exc}", "warning")

                # --- Broadcast presence (every 12 cycles ~60s) ---
                presence_counter += 1
                if presence and presence_counter >= 12:
                    presence_counter = 0
                    try:
                        self._broadcast_presence(skcomms, identity, presence)
                        self.last_heartbeat_at = datetime.now(timezone.utc)
                    except Exception as exc:
                        logger.warning("daemon.py: %s", exc)
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
                        logger.warning("daemon.py: %s", exc)
                        self._log(f"MemoryBridge auto-capture error: {exc}", "warning")

                # --- Watchdog health check + stats file write (every 6 cycles ~30s) ---
                watchdog_counter += 1
                if watchdog_counter >= 6:
                    watchdog_counter = 0
                    if watchdog:
                        try:
                            watchdog.check()
                        except Exception as exc:
                            logger.warning("daemon.py: %s", exc)
                            self._log(f"Watchdog error: {exc}", "warning")
                    try:
                        self._write_daemon_stats(watchdog, presence, skcomms)
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
                    self._broadcast_presence(skcomms, identity, presence, going_offline=True)
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
            logger.warning("daemon.py: %s", exc)
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
            logger.warning("daemon.py: %s", exc)
            self._log(f"Presence init skipped: {exc}", "warning")
            return None

    def _init_queue(self, skcomms: object, identity: str) -> object:
        """Initialize the outbox message queue for retry delivery.

        Also initialises the AgentMessenger stored on self._outbox_messenger
        so deliver_pending() has a send channel without re-creating it each cycle.

        Args:
            skcomms: SKComms instance.
            identity: Local CapAuth identity URI.

        Returns:
            OutboxQueue or None if initialization fails.
        """
        try:
            from .agent_comm import AgentMessenger
            from .outbox import OutboxQueue

            queue = OutboxQueue()
            try:
                self._outbox_messenger = AgentMessenger.from_identity(identity, skcomms=skcomms)
            except Exception as exc:
                logger.warning("daemon.py: %s", exc)
                self._log(f"Outbox messenger init skipped: {exc}", "warning")
            return queue
        except Exception as exc:
            logger.warning("daemon.py: %s", exc)
            self._log(f"Queue init skipped: {exc}", "warning")
            return None

    def _init_webrtc(self, skcomms: object, identity: str) -> None:
        """Wire the WebRTC transport to the chat daemon if available.

        Finds the WebRTC transport in the SKComms router and starts it
        if it hasn't been started yet. Stores incoming WEBRTC_SIGNAL
        envelopes as chat messages in the history for call management.

        Args:
            skcomms: Initialized SKComms instance.
            identity: Local identity URI (for call routing).
        """
        try:
            webrtc_transport = None
            for t in skcomms.router.transports:
                if t.name == "webrtc":
                    webrtc_transport = t
                    break

            if webrtc_transport is None:
                return

            # Surface a missing TURN secret early — non-fatal. Without it,
            # relayed (coturn) calls can't authenticate; STUN/LAN still works.
            turn_warning = webrtc_turn_warning()
            if turn_warning:
                self._log(turn_warning, "warning")

            # Start the transport if not already running
            if hasattr(webrtc_transport, "start") and not webrtc_transport._running:
                webrtc_transport.start()

            self._webrtc_active = True
            self._log("WebRTC transport wired (signaling connected on next poll)")

        except Exception as exc:
            logger.warning("daemon.py: %s", exc)
            self._log(f"WebRTC init skipped: {exc}", "warning")

    def _init_attachments(self, history: object, identity: str, skcomms: object) -> object:
        """Wire the receive-side AttachmentService into a FileTransferService.

        Builds a *persistent* FileTransferService (the one the poll loop routes
        incoming FILE_TRANSFER_INIT / FILE_CHUNK / FILE_TRANSFER_DONE messages
        through) and an AttachmentService over the same identity + history, then
        binds ``AttachmentService.on_transfer_complete`` as the file_service's
        ``on_complete`` callback.  When a transfer completes, the assembled file
        is posted as an inbound chat message so received images/files appear in
        the recipient's conversation.

        Stores both on ``self._file_service`` / ``self._attachment_service``.

        Failure is NON-FATAL — the daemon keeps running without attachment
        wiring if any step raises.

        Args:
            history: ChatHistory instance (where inbound messages are saved).
            identity: Local CapAuth identity URI (recipient of inbound files).
            skcomms: Initialized SKComms instance (transport for the service).

        Returns:
            AttachmentService or None if initialization fails.
        """
        try:
            from .attachments import AttachmentService
            from .files import FileTransferService

            file_service = FileTransferService(identity, skcomms=skcomms)
            attach = AttachmentService(
                identity=identity,
                history=history,
                file_service=file_service,
            )
            attach.bind(file_service)
            self._file_service = file_service
            self._attachment_service = attach
            return attach
        except Exception as exc:
            logger.warning("daemon.py: %s", exc)
            self._log(f"AttachmentService init skipped: {exc}", "warning")
            self._file_service = None
            self._attachment_service = None
            return None

    def _route_file_message(self, msg: "ChatMessage") -> bool:
        """Route a FILE_TRANSFER_* message to the persistent FileTransferService.

        Incoming file-transfer envelopes ride as JSON dicts (``{"type":
        "FILE_TRANSFER_INIT" | "FILE_CHUNK" | "FILE_TRANSFER_DONE", ...}``) and
        are surfaced by the transport as plain-text ChatMessages.  This forwards
        them to ``store_incoming_chunk`` so chunks are persisted and a completed
        transfer fires the bound ``on_complete`` (posting an inbound message).

        Returns True if the message was a recognised file-transfer envelope and
        was handed off (so the poll loop can skip normal processing for it).
        """
        if self._file_service is None:
            return False
        content = getattr(msg, "content", "") or ""
        if "FILE_TRANSFER_INIT" not in content and "FILE_CHUNK" not in content \
                and "FILE_TRANSFER_DONE" not in content:
            return False
        try:
            payload = json.loads(content)
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        msg_type = payload.get("type", "")
        if msg_type not in ("FILE_TRANSFER_INIT", "FILE_CHUNK", "FILE_TRANSFER_DONE"):
            return False
        # Carry the envelope sender through so on_complete can attribute the
        # inbound message to the right peer.
        payload.setdefault("sender", getattr(msg, "sender", ""))
        try:
            self._file_service.store_incoming_chunk(payload)
        except Exception as exc:
            logger.warning("file-transfer routing failed: %s", exc)
        return True

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
            logger.warning("daemon.py: %s", exc)
            self._log(f"MemoryBridge init skipped: {exc}", "warning")
            return None

    def _broadcast_presence(
        self,
        skcomms: object,
        identity: str,
        tracker: object,
        going_offline: bool = False,
    ) -> None:
        """Broadcast presence state over SKComms.

        Args:
            skcomms: SKComms instance.
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
            from skcomms.models import MessageType

            skcomms.send(
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
                        f"# HELP skchat_uptime_seconds Daemon uptime in seconds\n"
                        f"# TYPE skchat_uptime_seconds gauge\n"
                        f'skchat_uptime_seconds{{identity="{identity}"}} {uptime_s}\n'
                        f"# HELP skchat_messages_received_total Total messages received\n"
                        f"# TYPE skchat_messages_received_total counter\n"
                        f'skchat_messages_received_total{{identity="{identity}"}} {daemon_ref.total_received}\n'
                        f"# HELP skchat_messages_sent_total Total messages sent\n"
                        f"# TYPE skchat_messages_sent_total counter\n"
                        f'skchat_messages_sent_total{{identity="{identity}"}} {getattr(daemon_ref, "total_sent", 0)}\n'
                        f"# HELP skchat_advocacy_responses_total Total advocacy auto-responses\n"
                        f"# TYPE skchat_advocacy_responses_total counter\n"
                        f'skchat_advocacy_responses_total{{identity="{identity}"}} {advocacy_responses}\n'
                        f"# HELP skchat_peers_online Number of online peers\n"
                        f"# TYPE skchat_peers_online gauge\n"
                        f'skchat_peers_online{{identity="{identity}"}} {online_peers}\n'
                        f"# HELP skchat_transport_ok Transport health (1=ok, 0=down)\n"
                        f"# TYPE skchat_transport_ok gauge\n"
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
                    daemon_ref.last_poll_time.isoformat() if daemon_ref.last_poll_time else None
                )

                body = json.dumps(
                    {
                        "status": "ok" if daemon_ref.running else "stopping",
                        "uptime_s": uptime_s,
                        "messages_received": daemon_ref.total_received,
                        "last_poll_at": last_poll_at,
                        "transport_ok": daemon_ref._transport_ok,
                    }
                ).encode()

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

    def _init_watchdog(self, skcomms: object) -> object:
        """Initialize the transport watchdog.

        Args:
            skcomms: SKComms instance to monitor and reconnect.

        Returns:
            TransportWatchdog or None if initialization fails.
        """
        try:
            from .watchdog import TransportWatchdog

            return TransportWatchdog(transport=skcomms)
        except Exception as exc:
            logger.warning("daemon.py: %s", exc)
            self._log(f"Watchdog init skipped: {exc}", "warning")
            return None

    def _write_daemon_stats(
        self,
        watchdog: object,
        presence: object,
        skcomms: object,
    ) -> None:
        """Write daemon runtime stats to the stats JSON file.

        Stats are consumed by daemon_status() and the MCP daemon_status tool
        to expose uptime, message counts, and transport health across processes.

        Args:
            watchdog: TransportWatchdog instance (or None).
            presence: PresenceTracker instance (or None).
            skcomms: SKComms instance (or None).
        """
        stats_path = _DAEMON_STATS_FILE.expanduser()

        uptime_seconds = 0
        if self.start_time:
            uptime_seconds = int((datetime.now(timezone.utc) - self.start_time).total_seconds())

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
        if skcomms:
            try:
                for t in skcomms.router.transports:
                    if t.name == "webrtc":
                        webrtc_signaling_ok = bool(getattr(t, "_signaling_connected", False))
                        break
            except Exception as exc:
                logger.warning("Failed to read WebRTC signaling state: %s", exc)

        signaling_health = webrtc_signaling_health(
            webrtc_active=self._webrtc_active,
            signaling_connected=webrtc_signaling_ok,
        )
        if self._webrtc_active and signaling_health != "ok":
            logger.warning("WebRTC signaling %s — relayed calls may fall back", signaling_health)

        stats = {
            "uptime_seconds": uptime_seconds,
            "messages_sent": self.total_sent,
            "messages_received": self.total_received,
            "transport_status": transport_status,
            "webrtc_signaling_ok": webrtc_signaling_ok,
            "webrtc_signaling_health": signaling_health,
            "last_heartbeat_at": (
                self.last_heartbeat_at.isoformat() if self.last_heartbeat_at else None
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
DAEMON_LOCK_FILE = Path("~/.skchat/daemon.lock")
_DAEMON_STATS_FILE = DAEMON_STATS_FILE  # internal alias used by _write_daemon_stats

# Module-global holder for the single-instance flock. The lock is held for the
# life of the daemon process; this reference keeps the file object (and thus the
# lock) alive so it isn't garbage-collected and silently released.
_daemon_lock_handle = None

# How the running daemon subprocess identifies itself in the process table.
_DAEMON_PROC_MARKER = "skchat._daemon_entry"


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


def _live_daemon_pids() -> list[int]:
    """Return PIDs of all running skchat receive-daemon processes.

    Scans the process table (``/proc``) for the daemon subprocess marker rather
    than trusting the PID file. This is what makes single-instance enforcement
    robust: it detects a systemd-managed daemon even when the PID file is stale,
    missing, or was clobbered by a competing manual ``skchat daemon start`` (the
    duplicate-daemon trap). Excludes the current process.

    Linux-only (reads ``/proc``); returns ``[]`` where ``/proc`` is unavailable,
    in which case the flock in :func:`run_daemon` remains the backstop.

    Returns:
        list[int]: PIDs of live daemon subprocesses (may be empty).
    """
    import os

    pids: list[int] = []
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return pids
    self_pid = os.getpid()
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == self_pid:
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                "utf-8", "ignore"
            )
        except OSError:
            continue  # process vanished or unreadable — skip
        if _DAEMON_PROC_MARKER in cmdline:
            pids.append(pid)
    return pids


def is_running() -> bool:
    """Check if the daemon process is currently running (PID-file based).

    Fast, PID-file-only check used for status and the friendly start pre-check.
    It is deliberately NOT process-table aware: during a systemd restart the
    just-killed predecessor may briefly linger, and a process-scan here would
    race-block the legitimate new start. Authoritative single-instance
    enforcement is the flock in :func:`run_daemon` (race-free: the kernel
    releases the lock the instant the holder dies). For the desync case — a
    stale/clobbered PID file while a daemon really is alive — the flock still
    refuses a duplicate even though this returns False.

    Returns:
        bool: True if the PID-file process is alive, False otherwise.
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


def _acquire_singleton_lock(retries: int = 6, retry_delay: float = 0.4) -> bool:
    """Acquire the exclusive single-instance lock for the daemon.

    Takes a non-blocking ``flock`` on ``~/.skchat/daemon.lock`` and holds it for
    the life of the process (the handle is stashed in the module global
    ``_daemon_lock_handle`` so it isn't GC'd). This is the authoritative
    single-instance guard: OS-enforced and race-free, and the kernel releases it
    automatically when the holder dies — no stale-lock cleanup needed. A second
    daemon simply cannot acquire it.

    The acquisition is retried briefly so a systemd restart absorbs the kill →
    start race: the new daemon waits out a predecessor that is still shutting
    down rather than failing immediately. Total wait ≈ ``retries * retry_delay``.

    Args:
        retries: Additional attempts after the first if the lock is held.
        retry_delay: Seconds to sleep between attempts.

    Returns:
        bool: True if the lock was acquired (this process may run), False if
        another daemon still holds it after all retries. On platforms without
        ``fcntl`` the lock is skipped (returns True).
    """
    global _daemon_lock_handle

    try:
        import fcntl
    except ImportError:  # non-POSIX — best effort, no OS-level lock available
        return True

    lock_path = DAEMON_LOCK_FILE.expanduser()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "w")
    for attempt in range(retries + 1):
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            _daemon_lock_handle = handle  # hold the lock for the process lifetime
            return True
        except OSError:
            if attempt < retries:
                time.sleep(retry_delay)
    handle.close()
    return False


def _singleton_lock_held() -> bool:
    """Return True if another process currently holds the daemon singleton lock.

    Non-destructive probe: tries to grab the flock without blocking and, on
    success, immediately releases it. A held lock means a daemon is alive even
    when the PID file is stale (the desync case), so the launcher can refuse
    *before* forking — avoiding even a short-lived duplicate process. Safe for
    the systemd restart path: the predecessor is fully reaped before the new
    ExecStart runs, so the lock is free and this returns False.

    Returns:
        bool: True if held by another process; False if free (or no ``fcntl``).
    """
    try:
        import fcntl
    except ImportError:
        return False
    lock_path = DAEMON_LOCK_FILE.expanduser()
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True  # another process holds it
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def daemon_status() -> dict:
    """Get the current daemon status including runtime statistics.

    Reads the PID file to determine if the daemon is running, then merges
    in the stats written by ChatDaemon._write_daemon_stats (uptime,
    message counts, transport health, heartbeat time, online peers).

    Returns:
        dict with keys: running, pid, pid_file, log_file, plus
        uptime_seconds, messages_sent, messages_received,
        transport_status, webrtc_signaling_ok, webrtc_signaling_health,
        last_heartbeat_at, online_peer_count, updated_at
        (when daemon was last running).
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

    # Pre-fork guards (both non-racy w.r.t. systemd restart, since the
    # predecessor is fully reaped before the new ExecStart runs):
    #   1. PID-file check — fast path when the file is in sync.
    #   2. Singleton-lock probe — desync-proof: catches a live daemon even when
    #      the PID file is stale, so we refuse before forking a (doomed) child.
    # The flock acquired by the daemon process itself remains the authoritative
    # backstop against races the probe can't see.
    if is_running():
        raise RuntimeError(f"Daemon already running (PID {_read_pid()})")
    if _singleton_lock_held():
        others = _live_daemon_pids()
        raise RuntimeError(
            f"Daemon already running (PID {others[0] if others else 'unknown'})"
        )

    if not background:
        if not _acquire_singleton_lock():
            others = _live_daemon_pids()
            raise RuntimeError(
                "Daemon already running "
                f"(PID {others[0] if others else 'unknown'})"
            )
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
        sys.executable,
        "-m",
        "skchat._daemon_entry",
        "--interval",
        str(interval),
        "--log-file",
        str(log_path),
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

    # Single-instance guard (authoritative): refuse to run if another daemon
    # already holds the lock. Exit 0 — a duplicate launch is not a failure, the
    # daemon IS running, and we must not trip systemd's Restart=on-failure.
    if not _acquire_singleton_lock():
        others = _live_daemon_pids()
        msg = (
            "Another skchat daemon already holds the lock "
            f"(PID {others[0] if others else 'unknown'}); not starting a duplicate."
        )
        logger.warning(msg)
        print(f"  {msg}")
        sys.exit(0)

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
