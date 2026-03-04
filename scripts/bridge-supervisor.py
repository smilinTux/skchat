#!/usr/bin/env python3
"""bridge-supervisor.py — Systemd service health monitor for SKChat bridges.

Checks skchat-opus-bridge and skchat-lumina-bridge systemd user services
every 30 seconds and restarts any that are not active.

Usage:
  python3 scripts/bridge-supervisor.py              # continuous loop
  python3 scripts/bridge-supervisor.py --check-once # one-shot health check
  systemctl --user start skchat-bridge-supervisor.service
"""

from __future__ import annotations

import argparse
import logging
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

CHECK_INTERVAL = 30  # seconds between liveness checks

BRIDGE_SERVICES: list[str] = [
    "skchat-opus-bridge",
    "skchat-lumina-bridge",
]

# ─── Systemd helpers ─────────────────────────────────────────────────────────


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
    )


def _is_active(service: str) -> bool:
    return _systemctl("is-active", "--quiet", service).returncode == 0


def _unit_exists(service: str) -> bool:
    """Return True when systemd has a unit file for this service."""
    r = _systemctl("cat", service)
    return r.returncode == 0


def _restart(service: str) -> bool:
    r = _systemctl("restart", service)
    if r.returncode == 0:
        logger.info("Restarted %s successfully", service)
        return True
    logger.error(
        "Failed to restart %s: %s",
        service,
        (r.stderr or r.stdout).strip() or "(no output)",
    )
    return False


# ─── Core check ──────────────────────────────────────────────────────────────

# Possible status values returned by check_bridges():
#   "active"          — service is running normally
#   "restarted"       — was dead, restart succeeded
#   "restart-failed"  — was dead, restart command failed
#   "not-found"       — no unit file exists (skip silently after first warning)

_warned_missing: set[str] = set()


def check_bridges() -> dict[str, str]:
    """Check all bridge services. Returns {service: status} mapping."""
    results: dict[str, str] = {}

    for service in BRIDGE_SERVICES:
        if not _unit_exists(service):
            if service not in _warned_missing:
                logger.warning("Unit file not found for %s — skipping", service)
                _warned_missing.add(service)
            results[service] = "not-found"
            continue

        if _is_active(service):
            logger.debug("%s is active", service)
            results[service] = "active"
        else:
            logger.warning("%s is not active — restarting", service)
            ok = _restart(service)
            results[service] = "restarted" if ok else "restart-failed"

    return results


# ─── Entry point ─────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SKChat bridge supervisor — monitors systemd bridge services"
    )
    parser.add_argument(
        "--check-once",
        action="store_true",
        help="Perform a single health check and exit (non-zero exit if any service "
        "is unhealthy and could not be restarted)",
    )
    args = parser.parse_args()

    if args.check_once:
        logger.info("=== bridge-supervisor --check-once ===")
        results = check_bridges()
        print("\nBridge health check:")
        for service, status in results.items():
            if status == "active":
                symbol = "[OK]"
            elif status == "restarted":
                symbol = "[RESTARTED]"
            elif status == "not-found":
                symbol = "[NOT FOUND]"
            else:
                symbol = "[FAILED]"
            print(f"  {symbol} {service}: {status}")
        print(f"\nLog: {LOG_FILE}")
        unhealthy = [s for s, st in results.items() if st == "restart-failed"]
        sys.exit(1 if unhealthy else 0)

    # ── Continuous loop ───────────────────────────────────────────────────────
    logger.info(
        "Bridge supervisor starting (interval: %ds, log: %s)", CHECK_INTERVAL, LOG_FILE
    )
    logger.info("Monitoring: %s", ", ".join(BRIDGE_SERVICES))

    while True:
        try:
            check_bridges()
            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt — stopping")
            sys.exit(0)


if __name__ == "__main__":
    main()
