"""Tests for the truncate_middle and humanize_bytes text helpers."""

from __future__ import annotations

from skchat.textutil import humanize_bytes, truncate_middle


class TestTruncateMiddle:
    """Tests for truncate_middle."""

    def test_short_string_unchanged(self) -> None:
        """Strings at or under max_len are returned unchanged."""
        assert truncate_middle("hello", max_len=40) == "hello"

    def test_string_exactly_at_max_len_unchanged(self) -> None:
        """A string exactly max_len long is returned unchanged."""
        value = "a" * 40
        assert truncate_middle(value, max_len=40) == value

    def test_long_string_truncated_to_exact_length(self) -> None:
        """Long strings are shortened to exactly max_len characters."""
        value = "a" * 20 + "b" * 20 + "c" * 20
        result = truncate_middle(value, max_len=40)
        assert len(result) == 40
        assert result.startswith("a")
        assert result.endswith("c")

    def test_long_string_keeps_head_and_tail(self) -> None:
        """The head and tail of the string are preserved around the ellipsis."""
        value = "0123456789" * 10
        result = truncate_middle(value, max_len=21)
        assert len(result) == 21
        assert result.startswith(value[:10])
        assert result.endswith(value[-10:])

    def test_empty_string_returns_empty(self) -> None:
        """Empty input returns empty output without raising."""
        assert truncate_middle("", max_len=40) == ""

    def test_none_input_returns_empty(self) -> None:
        """None input never raises and returns empty string."""
        assert truncate_middle(None, max_len=40) == ""  # type: ignore[arg-type]

    def test_default_max_len(self) -> None:
        """Default max_len of 40 applies when not specified."""
        value = "x" * 100
        result = truncate_middle(value)
        assert len(result) == 40

    def test_max_len_zero_returns_empty(self) -> None:
        """A max_len of zero leaves no room for content and returns ''."""
        assert truncate_middle("abcdef", max_len=0) == ""

    def test_negative_max_len_returns_empty(self) -> None:
        """A negative max_len is treated the same as zero."""
        assert truncate_middle("abcdef", max_len=-5) == ""

    def test_max_len_one_returns_bare_ellipsis(self) -> None:
        """max_len=1 leaves no room for head/tail chars, just the ellipsis."""
        result = truncate_middle("abcdef", max_len=1)
        assert result == "…"
        assert len(result) == 1


class TestHumanizeBytes:
    """Tests for humanize_bytes."""

    def test_zero_bytes(self) -> None:
        """Zero bytes formats as a whole-number 'B' value."""
        assert humanize_bytes(0) == "0 B"

    def test_small_byte_count(self) -> None:
        """Byte counts under 1 KB show as whole 'B', no decimal."""
        assert humanize_bytes(512) == "512 B"

    def test_bytes_boundary_just_under_kb(self) -> None:
        """1023 bytes stays in the B unit."""
        assert humanize_bytes(1023) == "1023 B"

    def test_kb_boundary(self) -> None:
        """Exactly 1024 bytes crosses into KB."""
        assert humanize_bytes(1024) == "1.0 KB"

    def test_kb_with_fraction(self) -> None:
        """1536 bytes is 1.5 KB, one decimal place."""
        assert humanize_bytes(1536) == "1.5 KB"

    def test_mb_boundary(self) -> None:
        """1048576 bytes (1024 KB) crosses into MB."""
        assert humanize_bytes(1048576) == "1.0 MB"

    def test_gb_boundary(self) -> None:
        """1024**3 bytes crosses into GB."""
        assert humanize_bytes(1024**3) == "1.0 GB"

    def test_tb_boundary(self) -> None:
        """1024**4 bytes crosses into TB."""
        assert humanize_bytes(1024**4) == "1.0 TB"

    def test_beyond_tb_stays_in_tb(self) -> None:
        """There is no unit past TB; large values keep scaling within it."""
        assert humanize_bytes(1024**5) == "1024.0 TB"

    def test_none_input_returns_zero_bytes(self) -> None:
        """None input never raises and returns '0 B'."""
        assert humanize_bytes(None) == "0 B"

    def test_non_numeric_input_returns_zero_bytes(self) -> None:
        """Non-numeric input never raises and returns '0 B'."""
        assert humanize_bytes("not a number") == "0 B"  # type: ignore[arg-type]

    def test_negative_input_masked_to_zero_bytes(self) -> None:
        """Negative byte counts are masked to '0 B' rather than raising."""
        assert humanize_bytes(-1024) == "0 B"

    def test_float_input_does_not_raise(self) -> None:
        """Non-integer numeric input is accepted and formatted."""
        assert humanize_bytes(1536.0) == "1.5 KB"
