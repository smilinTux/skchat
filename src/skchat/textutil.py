"""Text shortening helpers for logs and UI display.

Long identifiers and URLs (capauth URIs, message ids, file paths) blow out
log lines and table columns. ``truncate_middle`` shortens them while keeping
both ends legible, which is usually what a reader needs to recognize a value
at a glance.
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


_BYTE_UNITS = ("B", "KB", "MB", "GB")


def humanize_bytes(n: float | None) -> str:
    """Format a byte count as a human-readable string (base 1024).

    Whole numbers are used for the ``B`` unit; ``KB`` and above show one
    decimal place. ``None`` and non-numeric input never raise, they return
    ``"0 B"``. Negative input is masked to ``"0 B"`` rather than raising or
    displaying a sign, since a negative size is never meaningful here.
    """
    try:
        size = float(n)
    except (TypeError, ValueError):
        return "0 B"
    if size < 0:
        return "0 B"

    unit = "B"
    for unit in _BYTE_UNITS:
        if size < 1024:
            break
        size /= 1024
    else:
        unit = "TB"

    if unit == "B":
        return f"{int(size)} B"
    return f"{size:.1f} {unit}"
