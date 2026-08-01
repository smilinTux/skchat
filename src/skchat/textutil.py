"""Text shortening and formatting helpers for logs and UI display.

Long identifiers and URLs (capauth URIs, message ids, file paths) blow out
log lines and table columns. ``truncate_middle`` shortens them while keeping
both ends legible, which is usually what a reader needs to recognize a value
at a glance. ``humanize_duration`` formats raw second counts (call/transfer
durations) into a compact human string.
"""

from __future__ import annotations

_DURATION_UNITS = (
    ("d", 86400),
    ("h", 3600),
    ("m", 60),
    ("s", 1),
)


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


def humanize_duration(seconds: float | None) -> str:
    """Format `seconds` as a compact human string, e.g. ``"1h 1m"``.

    Shows at most the two largest non-zero units (days, hours, minutes,
    seconds); units below the second-largest shown are dropped, not
    rounded. Non-numeric, ``None``, and negative input never raise and
    return ``"0s"``.
    """
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds < 0:
        return "0s"

    remaining = int(seconds)
    parts = []
    for suffix, unit_seconds in _DURATION_UNITS:
        value, remaining = divmod(remaining, unit_seconds)
        if value:
            parts.append(f"{value}{suffix}")
        if len(parts) == 2:
            break

    return " ".join(parts) if parts else "0s"
