"""Tests for the truncate_middle and humanize_duration text helpers."""

from __future__ import annotations

from skchat.textutil import humanize_duration, truncate_middle


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


class TestHumanizeDuration:
    """Tests for humanize_duration."""

    def test_zero_seconds(self) -> None:
        """Zero seconds formats as '0s'."""
        assert humanize_duration(0) == "0s"

    def test_seconds_only(self) -> None:
        """Sub-minute durations show only seconds."""
        assert humanize_duration(45) == "45s"

    def test_minutes_and_seconds(self) -> None:
        """Minutes and seconds are both shown when both are non-zero."""
        assert humanize_duration(90) == "1m 30s"

    def test_hours_and_minutes(self) -> None:
        """Only the two largest non-zero units are shown; seconds are dropped."""
        assert humanize_duration(3661) == "1h 1m"

    def test_days_and_hours(self) -> None:
        """Only the two largest non-zero units are shown; minutes/seconds are dropped."""
        assert humanize_duration(90000) == "1d 1h"

    def test_exact_minute_drops_seconds(self) -> None:
        """A whole number of minutes has no seconds component to show."""
        assert humanize_duration(120) == "2m"

    def test_none_returns_zero_seconds(self) -> None:
        """None input never raises and returns '0s'."""
        assert humanize_duration(None) == "0s"  # type: ignore[arg-type]

    def test_negative_returns_zero_seconds(self) -> None:
        """Negative input never raises and returns '0s'."""
        assert humanize_duration(-5) == "0s"

    def test_non_numeric_returns_zero_seconds(self) -> None:
        """Non-numeric input never raises and returns '0s'."""
        assert humanize_duration("abc") == "0s"  # type: ignore[arg-type]
