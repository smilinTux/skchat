"""Entry point for the skchat background daemon subprocess.

Called by start_daemon() when launching as a background process.
Not intended to be used directly.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    """Parse args and run the foreground daemon loop.

    Args are passed by start_daemon() via subprocess.Popen.
    """
    parser = argparse.ArgumentParser(description="skchat daemon subprocess")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--log-file", default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    from .daemon import run_daemon
    run_daemon(
        interval=args.interval,
        log_file=args.log_file,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    main()
