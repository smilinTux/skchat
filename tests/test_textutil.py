"""Tests for the truncate_middle, humanize_bytes, and format_relative_time text helpers."""

from __future__ import annotations

from skchat.textutil import (
    format_relative_time,
    humanize_bytes,
    humanize_duration,
    truncate_middle,
)


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


class TestFormatRelativeTime:
    """Tests for format_relative_time."""

    NOW = "2026-07-27T12:00:00"

    def test_under_a_minute_is_now(self) -> None:
        """Deltas under 60s format as 'now'."""
        assert format_relative_time("2026-07-27T11:59:30", self.NOW) == "now"

    def test_zero_delta_is_now(self) -> None:
        """A timestamp equal to now formats as 'now'."""
        assert format_relative_time(self.NOW, self.NOW) == "now"

    def test_minute_boundary_just_under_returns_now(self) -> None:
        """59 seconds ago is still 'now'."""
        assert format_relative_time("2026-07-27T11:59:01", self.NOW) == "now"

    def test_minute_boundary_at_60s_returns_minutes(self) -> None:
        """Exactly 60 seconds ago crosses into minutes."""
        assert format_relative_time("2026-07-27T11:59:00", self.NOW) == "1m"

    def test_minutes(self) -> None:
        """Deltas under an hour format as whole minutes."""
        assert format_relative_time("2026-07-27T11:55:00", self.NOW) == "5m"

    def test_hour_boundary_just_under_returns_minutes(self) -> None:
        """59m59s ago is still minutes."""
        assert format_relative_time("2026-07-27T11:00:01", self.NOW) == "59m"

    def test_hour_boundary_at_3600s_returns_hours(self) -> None:
        """Exactly 3600 seconds ago crosses into hours."""
        assert format_relative_time("2026-07-27T11:00:00", self.NOW) == "1h"

    def test_hours(self) -> None:
        """Deltas under a day format as whole hours."""
        assert format_relative_time("2026-07-27T09:00:00", self.NOW) == "3h"

    def test_day_boundary_just_under_returns_hours(self) -> None:
        """23h59m59s ago is still hours."""
        assert format_relative_time("2026-07-26T12:00:01", self.NOW) == "23h"

    def test_day_boundary_at_86400s_returns_days(self) -> None:
        """Exactly 86400 seconds ago crosses into days."""
        assert format_relative_time("2026-07-26T12:00:00", self.NOW) == "1d"

    def test_days(self) -> None:
        """Deltas under a week format as whole days."""
        assert format_relative_time("2026-07-25T12:00:00", self.NOW) == "2d"

    def test_week_boundary_just_under_returns_days(self) -> None:
        """6d23h59m59s ago is still days."""
        assert format_relative_time("2026-07-20T12:00:01", self.NOW) == "6d"

    def test_week_boundary_at_604800s_returns_date(self) -> None:
        """Exactly 604800 seconds ago crosses into a date label."""
        assert format_relative_time("2026-07-20T12:00:00", self.NOW) == "Jul 20"

    def test_date_label_format(self) -> None:
        """Timestamps beyond a week format as 'Mon DD'."""
        assert format_relative_time("2026-06-15T12:00:00", self.NOW) == "Jun 15"

    def test_future_timestamp_returns_now(self) -> None:
        """A future ts (negative delta) is masked to 'now'."""
        assert format_relative_time("2026-07-27T13:00:00", self.NOW) == "now"

    def test_none_ts_returns_empty(self) -> None:
        """None iso_ts never raises and returns ''."""
        assert format_relative_time(None, self.NOW) == ""

    def test_none_now_returns_empty(self) -> None:
        """None now_iso never raises and returns ''."""
        assert format_relative_time(self.NOW, None) == ""

    def test_unparseable_ts_returns_empty(self) -> None:
        """An unparseable iso_ts never raises and returns ''."""
        assert format_relative_time("not-a-date", self.NOW) == ""

    def test_unparseable_now_returns_empty(self) -> None:
        """An unparseable now_iso never raises and returns ''."""
        assert format_relative_time(self.NOW, "not-a-date") == ""

    def test_empty_string_ts_returns_empty(self) -> None:
        """An empty-string iso_ts never raises and returns ''."""
        assert format_relative_time("", self.NOW) == ""


class TestHumanizeDuration:
    """Tests for humanize_duration."""

    def test_zero_seconds(self) -> None:
        """Zero seconds formats as '0s'."""
        assert humanize_duration(0) == "0s"

    def test_seconds_only(self) -> None:
        """Sub-minute durations show only seconds."""
        assert humanize_duration(45) == "45s"

    def test_minutes_and_seconds(self) -> None:
        """Durations under an hour show minutes and seconds."""
        assert humanize_duration(90) == "1m 30s"

    def test_hours_and_minutes_drops_seconds(self) -> None:
        """Durations with hours drop the seconds unit."""
        assert humanize_duration(3661) == "1h 1m"

    def test_days_and_hours_drops_minutes_and_seconds(self) -> None:
        """Durations with days drop minutes and seconds."""
        assert humanize_duration(90000) == "1d 1h"

    def test_exact_minute_drops_seconds(self) -> None:
        """A duration that is an exact number of minutes has no seconds unit."""
        assert humanize_duration(120) == "2m"

    def test_none_returns_zero(self) -> None:
        """None input never raises and returns '0s'."""
        assert humanize_duration(None) == "0s"  # type: ignore[arg-type]

    def test_negative_returns_zero(self) -> None:
        """Negative input never raises and returns '0s'."""
        assert humanize_duration(-5) == "0s"

    def test_non_numeric_returns_zero(self) -> None:
        """Non-numeric input never raises and returns '0s'."""
        assert humanize_duration("abc") == "0s"  # type: ignore[arg-type]
