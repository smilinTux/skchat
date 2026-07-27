"""Text formatting helpers for logs and UI display.

Long identifiers and URLs (capauth URIs, message ids, file paths) blow out
log lines and table columns. ``truncate_middle`` shortens them while keeping
both ends legible, which is usually what a reader needs to recognize a value
at a glance. ``humanize_duration`` formats raw seconds counts (call/transfer
durations) into a compact human string.
"""

from __future__ import annotations


def truncate_middle(value: str, max_len: int = 40) -> str:
    """Shorten `value` to `max_len` chars, eliding the middle.

    Keeps the head and tail of the string and joins them with a single
    ellipsis character. Falsy input (``None``, ``""``) returns ``""``.
    """
    if not value:
        return ""
    if len(value) <= max_len:
        return value
    if max_len <= 0:
        return ""

    available = max_len - 1
    head_len = -(-available // 2)  # ceil half
    tail_len = available - head_len
    tail = value[len(value) - tail_len :] if tail_len else ""
    return f"{value[:head_len]}…{tail}"


def humanize_duration(seconds: int | float) -> str:
    """Format a seconds count as a compact human string, e.g. ``'1h 1m'``.

    Shows at most the two largest non-zero units (days, hours, minutes,
    seconds); units below the second-largest shown are dropped, not rounded.
    Invalid input (``None``, non-numeric, negative) returns ``"0s"``.
    """
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "0s"
    if total <= 0:
        return "0s"

    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)

    units = [("d", days), ("h", hours), ("m", minutes), ("s", secs)]
    nonzero = [(suffix, value) for suffix, value in units if value]
    shown = nonzero[:2]
    return " ".join(f"{value}{suffix}" for suffix, value in shown)
