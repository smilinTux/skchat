"""Tests for the Space recording → transcript → write-up pipeline.

All seams (transcriber / summarizer / poster) are fakes — no real Whisper,
LLM, or network is touched.
"""

from __future__ import annotations

from skchat.spaces.recording_writeup import (
    FakeSummarizer,
    FakeTranscriber,
    RecordingWriteup,
    Summarizer,
    Transcriber,
    _fallback_writeup,
)


class _RecordingPoster:
    """A fake poster that records every (space_id, text) it is asked to post."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, space_id: str, text: str) -> None:
        self.calls.append((space_id, text))


def test_process_transcribes_summarizes_posts_and_returns() -> None:
    transcriber = FakeTranscriber("hello world this is the meeting")
    summarizer = FakeSummarizer()
    poster = _RecordingPoster()
    wu = RecordingWriteup()

    out = wu.process(
        "space-123",
        "/tmp/space-123.ogg",
        title="Weekly Sync",
        transcriber=transcriber,
        summarizer=summarizer,
        poster=poster,
    )

    # Transcriber saw the audio file.
    assert transcriber.seen == ["/tmp/space-123.ogg"]
    # Summarizer saw the transcript + title.
    assert summarizer.seen_transcript == "hello world this is the meeting"
    assert summarizer.seen_title == "Weekly Sync"
    # The write-up is a markdown doc with the expected sections.
    assert "## Summary" in out
    assert "## Key Points" in out
    assert "## Action Items" in out
    # Posted exactly once, with the write-up text, to the right space.
    assert len(poster.calls) == 1
    assert poster.calls[0][0] == "space-123"
    assert poster.calls[0][1] == out


def test_empty_transcript_is_handled_gracefully() -> None:
    transcriber = FakeTranscriber("")  # silence / no speech
    summarizer = FakeSummarizer()
    poster = _RecordingPoster()
    wu = RecordingWriteup()

    out = wu.process(
        "space-empty",
        "/tmp/space-empty.ogg",
        title="Quiet Room",
        transcriber=transcriber,
        summarizer=summarizer,
        poster=poster,
    )

    # No crash; summarizer was NOT invoked on empty input.
    assert summarizer.seen_transcript is None
    # Posted an honest "no audio/transcript" note (once).
    assert len(poster.calls) == 1
    assert poster.calls[0][0] == "space-empty"
    assert "no" in poster.calls[0][1].lower()
    # The returned note is what was posted.
    assert out == poster.calls[0][1]


def test_none_transcript_is_handled_like_empty() -> None:
    """A transcriber that returns None (failure) must not crash the pipeline."""

    class _NoneTranscriber:
        def transcribe(self, audio_path: str):  # noqa: ARG002
            return None

    poster = _RecordingPoster()
    wu = RecordingWriteup()
    out = wu.process(
        "space-none",
        "/tmp/x.ogg",
        title="t",
        transcriber=_NoneTranscriber(),
        summarizer=FakeSummarizer(),
        poster=poster,
    )
    assert len(poster.calls) == 1
    assert "no" in out.lower()


def test_fakes_satisfy_the_protocols() -> None:
    assert isinstance(FakeTranscriber("x"), Transcriber)
    assert isinstance(FakeSummarizer(), Summarizer)


def test_poster_default_is_overridable_via_constructor() -> None:
    """A poster set on the orchestrator is used when process() omits one."""
    poster = _RecordingPoster()
    wu = RecordingWriteup(poster=poster)
    out = wu.process(
        "space-ctor",
        "/tmp/a.ogg",
        title="Ctor",
        transcriber=FakeTranscriber("some words were spoken"),
        summarizer=FakeSummarizer(),
    )
    assert len(poster.calls) == 1
    assert poster.calls[0] == ("space-ctor", out)


# ---------------------------------------------------------------------------
# QA Area 2 — additional write-up pipeline coverage
# ---------------------------------------------------------------------------


def test_whitespace_only_transcript_is_treated_as_empty() -> None:
    """A transcript of only whitespace must NOT reach the summarizer."""
    summarizer = FakeSummarizer()
    poster = _RecordingPoster()
    out = RecordingWriteup().process(
        "space-ws",
        "/tmp/ws.ogg",
        title="Whitespace",
        transcriber=FakeTranscriber("   \n\t  "),
        summarizer=summarizer,
        poster=poster,
    )
    assert summarizer.seen_transcript is None
    assert "no" in out.lower()
    assert len(poster.calls) == 1


def test_transcriber_exception_does_not_crash_pipeline() -> None:
    """If the transcriber raises, the pipeline degrades to the no-transcript note
    rather than propagating the error."""

    class _BoomTranscriber:
        def transcribe(self, audio_path: str):  # noqa: ARG002
            raise RuntimeError("whisper exploded")

    poster = _RecordingPoster()
    out = RecordingWriteup().process(
        "space-boom",
        "/tmp/boom.ogg",
        title="Crashy",
        transcriber=_BoomTranscriber(),
        summarizer=FakeSummarizer(),
        poster=poster,
    )
    # No exception; honest note posted once.
    assert len(poster.calls) == 1
    assert "no" in out.lower()


def test_poster_failure_does_not_lose_the_writeup() -> None:
    """A poster that raises must not prevent process() from returning the text."""

    def _failing_poster(space_id: str, text: str) -> None:  # noqa: ARG001
        raise ConnectionError("spaces server down")

    out = RecordingWriteup().process(
        "space-fail",
        "/tmp/f.ogg",
        title="Resilient",
        transcriber=FakeTranscriber("words were said here"),
        summarizer=FakeSummarizer(),
        poster=_failing_poster,
    )
    # The write-up is still returned to the caller despite the post failing.
    assert "## Summary" in out
    assert "## Action Items" in out


def test_transcript_is_stripped_before_summarizing() -> None:
    """Leading/trailing whitespace is stripped before the summarizer sees it."""
    summarizer = FakeSummarizer()
    RecordingWriteup().process(
        "space-strip",
        "/tmp/s.ogg",
        title="Strip",
        transcriber=FakeTranscriber("  padded transcript  "),
        summarizer=summarizer,
        poster=_RecordingPoster(),
    )
    assert summarizer.seen_transcript == "padded transcript"


def test_per_call_seam_overrides_instance_default() -> None:
    """A per-call summarizer overrides one set on the instance."""
    instance_sm = FakeSummarizer()
    call_sm = FakeSummarizer()
    wu = RecordingWriteup(summarizer=instance_sm)
    wu.process(
        "space-ovr",
        "/tmp/o.ogg",
        title="Override",
        transcriber=FakeTranscriber("hello"),
        summarizer=call_sm,
        poster=_RecordingPoster(),
    )
    # The per-call summarizer ran; the instance one did not.
    assert call_sm.seen_transcript == "hello"
    assert instance_sm.seen_transcript is None


def test_fallback_writeup_has_required_sections_and_excerpt() -> None:
    """The no-LLM fallback always emits the three required markdown sections."""
    out = _fallback_writeup("a short transcript", title="Fallback Meeting")
    assert out.startswith("# Fallback Meeting")
    assert "## Summary" in out
    assert "## Key Points" in out
    assert "## Action Items" in out
    assert "a short transcript" in out


def test_fallback_writeup_truncates_long_transcript() -> None:
    """A very long transcript is truncated in the fallback excerpt (with ellipsis)."""
    long_text = "Z" * 2000
    out = _fallback_writeup(long_text, title="Long")
    assert "…" in out
    # The excerpt is truncated: far fewer than the 2000 input chars survive.
    assert out.count("Z") <= 800
    assert out.count("Z") < len(long_text)
