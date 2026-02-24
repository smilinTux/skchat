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

import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


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
            from skcomm import SKComm
            skcomm = SKComm.from_config()
        except Exception as exc:
            self._log(f"Failed to initialize SKComm: {exc}", "error")
            self._log("Make sure SKComm is configured: skcomm init", "error")
            raise

        try:
            history = ChatHistory.from_config()
        except Exception:
            from skmemory import MemoryStore
            history = ChatHistory(store=MemoryStore())

        identity = get_sovereign_identity()
        
        try:
            transport = ChatTransport(
                skcomm=skcomm,
                history=history,
                identity=identity,
            )
        except Exception as exc:
            self._log(f"Failed to initialize transport: {exc}", "error")
            raise

        self._log(f"SKChat daemon starting (identity: {identity})")
        self._log(f"Polling every {self.interval}s, Ctrl+C to stop")

        self.running = True

        try:
            while self.running:
                self.poll_count += 1
                self.last_poll_time = datetime.now(timezone.utc)

                try:
                    messages = transport.poll_inbox()
                    
                    if messages:
                        self.total_received += len(messages)
                        self._log(f"Received {len(messages)} message(s) (total: {self.total_received})")
                        
                        for msg in messages:
                            sender_short = msg.sender.split("@")[0].replace("capauth:", "")
                            preview = msg.content[:60] + ("..." if len(msg.content) > 60 else "")
                            self._log(f"  [{sender_short}] {preview}")
                    else:
                        if self.poll_count % 12 == 0:
                            self._log(f"No new messages (polls: {self.poll_count}, uptime: {self._uptime()})")

                except Exception as exc:
                    self._log(f"Poll error: {exc}", "warning")

                time.sleep(self.interval)

        except DaemonShutdown:
            pass
        except KeyboardInterrupt:
            pass
        finally:
            self._log(f"Daemon stopped. Received {self.total_received} message(s) total.")

    def _uptime(self) -> str:
        """Calculate daemon uptime.

        Returns:
            str: Human-readable uptime (e.g., "5m 30s")
        """
        if not self.last_poll_time:
            return "0s"

        uptime_seconds = int(self.poll_count * self.interval)
        
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

    @staticmethod
    def from_config(config_path: Optional[Path] = None) -> "ChatDaemon":
        """Create a daemon from configuration file.

        Args:
            config_path: Path to config file (default: ~/.skchat/config.yml)

        Returns:
            ChatDaemon: Configured daemon instance.
        """
        import os

        if config_path is None:
            config_path = Path.home() / ".skchat" / "config.yml"

        interval = float(os.environ.get("SKCHAT_DAEMON_INTERVAL", "5.0"))
        log_file = os.environ.get("SKCHAT_DAEMON_LOG")
        quiet = os.environ.get("SKCHAT_DAEMON_QUIET", "").lower() in ("1", "true", "yes")

        if config_path.exists():
            try:
                import yaml
                with open(config_path) as f:
                    cfg = yaml.safe_load(f) or {}
                
                daemon_cfg = cfg.get("daemon", {})
                interval = daemon_cfg.get("poll_interval", interval)
                log_file = daemon_cfg.get("log_file", log_file)
                quiet = daemon_cfg.get("quiet", quiet)
            except Exception:
                pass

        log_path = Path(log_file).expanduser() if log_file else None

        return ChatDaemon(
            interval=interval,
            log_file=log_path,
            quiet=quiet,
        )


def run_daemon(
    interval: float = 5.0,
    log_file: Optional[str] = None,
    quiet: bool = False,
) -> None:
    """Run the chat daemon with the specified configuration.

    This is the main entry point for the daemon functionality.

    Args:
        interval: Poll interval in seconds
        log_file: Optional path to log file
        quiet: If True, suppress console output

    Examples:
        >>> run_daemon(interval=10, log_file="~/.skchat/daemon.log")
    """
    log_path = Path(log_file).expanduser() if log_file else None
    daemon = ChatDaemon(interval=interval, log_file=log_path, quiet=quiet)
    
    try:
        daemon.start()
    except Exception as exc:
        logger.error(f"Daemon failed to start: {exc}")
        sys.exit(1)
