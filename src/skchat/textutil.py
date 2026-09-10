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


_BYTE_UNITS = ("B", "KB", "MB", "GB", "TB")


def humanize_bytes(n: int | float | None) -> str:
    """Format a byte count as a human-readable string (base 1024).

    Whole numbers for bytes, one decimal place for KB and above, e.g.
    ``0 -> "0 B"``, ``1536 -> "1.5 KB"``, ``1048576 -> "1.0 MB"``.

    Non-numeric input (``None`` or otherwise) never raises and returns
    ``"0 B"``. Negative input is masked to ``"0 B"`` rather than a signed
    form, since a negative byte count is never meaningful.
    """
    if not isinstance(n, (int, float)) or isinstance(n, bool) or n < 0:
        return "0 B"

    size = float(n)
    for unit in _BYTE_UNITS:
        if size < 1024 or unit == _BYTE_UNITS[-1]:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "0 B"  # pragma: no cover - unreachable, loop always returns
