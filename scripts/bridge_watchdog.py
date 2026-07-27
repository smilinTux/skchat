#!/usr/bin/env python3
"""systemd sd_notify heartbeat + poll-failure wedge detection for the
Telegram bridge (scripts/telegram_bridge.py).

The bridges wedge silently when a poll hangs on a Telegram ConnectTimeout:
the asyncio loop blocks forever inside the httpx connection pool while
systemd still sees the process as "active" (a plain Type=simple unit has no
liveness signal beyond "process exists"). This module gives the bridge two
systemd-facing signals, both stdlib-only (no new pip dependency):

  - sd_notify(): a minimal implementation of the sd_notify(3) protocol --
    write a datagram to $NOTIFY_SOCKET. A safe no-op when $NOTIFY_SOCKET is
    unset (not running under systemd Type=notify, e.g. `--check`, pytest, or
    a host that has not cut over to the watchdog drop-in yet), so the bridge
    behaves exactly the same outside a notify unit.
  - PollFailureTracker: a pure, stateful counter -- N consecutive poll
    failures (default 3) means the bridge should log, alert, and exit so
    systemd's Restart=on-failure brings it back clean instead of staying
    silently wedged. A single successful poll resets the counter to 0.
  - sk_alert(): best-effort Telegram/ops alert via the sk-alert CLI. Never
    raises -- a broken or missing sk-alert binary must never take the bridge
    itself down.

All three are exercised directly by tests/test_bridge_watchdog.py with no
live asyncio loop, no network, and (for sd_notify) a real but local AF_UNIX
datagram socket standing in for systemd's.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
from typing import Mapping, Optional

log = logging.getLogger("tg-bridge.watchdog")


def sd_notify(message: str, *, environ: Optional[Mapping[str, str]] = None) -> bool:
    """Send an sd_notify(3) datagram to $NOTIFY_SOCKET (e.g. "READY=1",
    "WATCHDOG=1").

    Best-effort: returns False and never raises when $NOTIFY_SOCKET is unset
    or the send fails for any reason -- the normal case whenever the process
    is not running under a systemd Type=notify unit. Returns True once the
    datagram has been handed to the socket.

    The environment is read at CALL time (default os.environ, overridable
    via ``environ`` for tests) rather than at import time, so behavior is
    correct no matter when systemd sets the variable relative to module
    import, and callers get a real per-call no-op when unset.
    """
    env = environ if environ is not None else os.environ
    addr = env.get("NOTIFY_SOCKET")
    if not addr:
        return False
    # Linux abstract socket namespace: an address starting with "@" maps to
    # a leading NUL byte, not a literal "@" path (see sd_notify(3), "Notes").
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    sock: Optional[socket.socket] = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.connect(addr)
        sock.sendall(message.encode("utf-8"))
        return True
    except OSError:
        log.debug("sd_notify(%r) failed (NOTIFY_SOCKET=%r)", message, addr, exc_info=True)
        return False
    finally:
        if sock is not None:
            sock.close()


class PollFailureTracker:
    """Consecutive-poll-failure counter and wedge-exit decision.

    Pure and stateful -- no I/O, no asyncio, no network -- so it is
    unit-testable without the live poll loop or a running bridge. A
    successful poll resets the counter to 0; ``threshold`` consecutive
    failures means the caller should treat the bridge as wedged and exit
    for systemd to restart it clean.
    """

    def __init__(self, threshold: int = 3):
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        self.threshold = threshold
        self.consecutive = 0

    def on_success(self) -> None:
        """A poll succeeded -- reset the consecutive-failure count."""
        self.consecutive = 0

    def on_failure(self) -> bool:
        """A poll failed (timed out or raised). Returns True once
        ``consecutive`` reaches ``threshold`` (the caller should exit for a
        clean systemd restart); False while still below it."""
        self.consecutive += 1
        return self.consecutive >= self.threshold


def sk_alert(message: str, *, binary: Optional[str] = None, timeout: float = 10.0) -> bool:
    """Best-effort sk-alert notification.

    Never raises: a missing binary, a hung sk-alert process, or a non-zero
    exit are all swallowed and logged at debug level -- the wedge-exit path
    that calls this must complete (log + sys.exit) even when alerting itself
    is broken. Returns True only when the process ran and exited 0.
    """
    binpath = binary or os.path.expanduser("~/.skenv/bin/sk-alert")
    try:
        result = subprocess.run(
            [binpath, message], timeout=timeout, check=False, capture_output=True
        )
        return result.returncode == 0
    except Exception:
        log.debug("sk-alert failed (binary=%r)", binpath, exc_info=True)
        return False
