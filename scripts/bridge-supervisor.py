#!/usr/bin/env python3
"""bridge-supervisor.py — Supervisor for SKChat bridge subprocesses.

Starts opus-bridge.py and lumina-bridge.py as subprocesses and keeps them
alive: if either dies it is logged and restarted after a 5-second delay.
Handles SIGTERM cleanly by forwarding it to both children before exiting.

Usage:
  python3 scripts/bridge-supervisor.py
  systemctl --user start skchat-bridge-supervisor.service
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# ─── Logging ─────────────────────────────────────────────────────────────────

LOG_DIR = Path.home() / ".skchat"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "bridge-supervisor.log"

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s")
_file_handler = logging.FileHandler(LOG_FILE)
_file_handler.setFormatter(_fmt)
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_fmt)

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stream_handler])
logger = logging.getLogger("bridge-supervisor")

# ─── Config ──────────────────────────────────────────────────────────────────

SCRIPTS_DIR = Path(__file__).parent
CHECK_INTERVAL = 10   # seconds between liveness checks
RESTART_DELAY = 5     # seconds to wait before restarting a dead child

BRIDGES: dict[str, Path] = {
    "opus-bridge": SCRIPTS_DIR / "opus-bridge.py",
    "lumina-bridge": SCRIPTS_DIR / "lumina-bridge.py",
}

# ─── State ───────────────────────────────────────────────────────────────────

_procs: dict[str, subprocess.Popen | None] = {name: None for name in BRIDGES}
_shutdown = False


# ─── Signal handling ──────────────────────────────────────────────────────────

def _handle_sigterm(signum: int, frame) -> None:  # noqa: ANN001
    global _shutdown
    logger.info("SIGTERM received — shutting down children")
    _shutdown = True
    for name, proc in _procs.items():
        if proc is not None and proc.poll() is None:
            logger.info("Terminating %s (pid=%d)", name, proc.pid)
            proc.terminate()
    # Give children a moment to exit gracefully
    time.sleep(2)
    for name, proc in _procs.items():
        if proc is not None and proc.poll() is None:
            logger.warning("Force-killing %s (pid=%d)", name, proc.pid)
            proc.kill()
    logger.info("All children stopped. Exiting.")
    sys.exit(0)


signal.signal(signal.SIGTERM, _handle_sigterm)


# ─── Process management ───────────────────────────────────────────────────────

def _start(name: str, script: Path) -> subprocess.Popen | None:
    """Launch a bridge script as a subprocess and return the Popen object."""
    if not script.exists():
        logger.warning("Script not found, skipping %s (%s)", name, script)
        return None
    try:
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
        )
        logger.info("Started %s (pid=%d)", name, proc.pid)
        return proc
    except Exception as exc:
        logger.error("Failed to start %s: %s", name, exc)
        return None


def check_and_restart() -> None:
    """Check liveness of each bridge; restart any that have died."""
    for name, script in BRIDGES.items():
        proc = _procs[name]

        if proc is None:
            # Never started (e.g. script missing on first attempt) — retry
            _procs[name] = _start(name, script)
            continue

        exit_code = proc.poll()
        if exit_code is not None:
            logger.error(
                "%s died (pid=%d, exit=%d) — restarting in %ds",
                name, proc.pid, exit_code, RESTART_DELAY,
            )
            time.sleep(RESTART_DELAY)
            _procs[name] = _start(name, script)


# ─── Entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("Bridge supervisor starting (check interval: %ds)", CHECK_INTERVAL)

    # Initial launch
    for name, script in BRIDGES.items():
        _procs[name] = _start(name, script)

    # Print startup summary
    started = {n: p.pid for n, p in _procs.items() if p is not None}
    skipped = [n for n, p in _procs.items() if p is None]
    print(
        f"[bridge-supervisor] running — "
        + ", ".join(f"{n} pid={pid}" for n, pid in started.items())
        + (f" | skipped: {', '.join(skipped)}" if skipped else ""),
        flush=True,
    )
    logger.info("Log file: %s", LOG_FILE)

    while not _shutdown:
        try:
            time.sleep(CHECK_INTERVAL)
            check_and_restart()
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt — shutting down")
            _handle_sigterm(signal.SIGINT, None)


if __name__ == "__main__":
    main()
