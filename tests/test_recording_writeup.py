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
