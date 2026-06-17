"""Space recording → AI transcript → LLM write-up → posted back to the Space.

When a Space's audio recording (Tier S3 egress, see :mod:`skchat.spaces.recording`)
finishes, this module turns the OGG file into a meeting write-up and posts it
back into the Space's **chat lane**.

Pipeline::

    audio.ogg ──Transcriber──▶ transcript ──Summarizer──▶ write-up.md ──poster──▶ chat lane

Everything is built around three clean seams so the orchestrator is unit-testable
with **no real Whisper, LLM, or network**:

* :class:`Transcriber` — ``transcribe(audio_path) -> str | None``.
  Real impl :class:`WhisperTranscriber` wraps the existing
  :class:`skchat.voice.VoiceRecorder` Whisper STT backend; :class:`FakeTranscriber`
  is the test double.
* :class:`Summarizer` — ``summarize(transcript, *, title) -> str`` returning a
  markdown write-up (``## Summary`` / ``## Key Points`` / ``## Action Items``).
  Real impl :class:`LLMSummarizer` calls the local OpenAI-compatible LLM
  (``SKCHAT_LLM_URL``, same convention as :mod:`skchat.voice_stream`);
  :class:`FakeSummarizer` is the test double.
* ``poster`` — a ``Callable[[space_id, text], None]``. Default
  :func:`chat_lane_poster` POSTs ``{"lane":"chat", ...}`` to
  ``/spaces/{id}/lanes/event``. Injectable so tests never hit the network.

The orchestrator handles a missing/empty/None transcript gracefully: it posts an
honest "no audio / no transcript" note instead of calling the LLM, and returns
that note.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Callable, Optional, Protocol, runtime_checkable

logger = logging.getLogger("skchat.spaces.recording_writeup")

#: Callable used to publish the write-up. ``(space_id, text) -> None``.
Poster = Callable[[str, str], None]

# Posting identity for the agent that authors the write-up.
_DEFAULT_AGENT = os.environ.get("SKAGENT", "lumina")
# Base URL of the skchat spaces server (where /spaces/{id}/lanes/event lives).
_SPACES_BASE = os.environ.get("SKCHAT_SPACES_URL", "http://127.0.0.1:9385")
# Local OpenAI-compatible LLM endpoint (same convention as voice_stream.py).
_LLM_URL = os.environ.get("SKCHAT_LLM_URL", "http://127.0.0.1:11434/v1/chat/completions")
_LLM_MODEL = os.environ.get("SKCHAT_LLM_MODEL", "qwen3.5:4b")


# ===========================================================================
# Seam 1: Transcriber
# ===========================================================================


@runtime_checkable
class Transcriber(Protocol):
    """Turn an audio file into a transcript string (or None if it can't)."""

    def transcribe(self, audio_path: str) -> Optional[str]: ...


class WhisperTranscriber:
    """Real transcriber wrapping the existing skchat Whisper STT backend.

    Delegates to :class:`skchat.voice.VoiceRecorder` (default backend = Whisper,
    overridable via ``SKCHAT_STT_BACKEND``), so this reuses the one sovereign
    on-device STT entry point rather than re-implementing it.
    """

    def __init__(self, *, whisper_model: str = "base", backend=None) -> None:
        from skchat.voice import VoiceRecorder

        self._recorder = VoiceRecorder(whisper_model=whisper_model, backend=backend)

    def transcribe(self, audio_path: str) -> Optional[str]:
        if not self._recorder.available:
            logger.warning("Whisper STT unavailable; cannot transcribe %s", audio_path)
            return None
        return self._recorder.backend.transcribe(audio_path)


class FakeTranscriber:
    """Test double — returns a canned transcript and records the paths it saw."""

    def __init__(self, transcript: str) -> None:
        self._transcript = transcript
        self.seen: list[str] = []

    def transcribe(self, audio_path: str) -> Optional[str]:
        self.seen.append(audio_path)
        return self._transcript


# ===========================================================================
# Seam 2: Summarizer
# ===========================================================================

_SUMMARY_SYSTEM = (
    "You are a meeting scribe. Given a raw transcript of a voice Space, write a "
    "concise markdown write-up. Use EXACTLY these three sections and headings, in "
    "this order:\n"
    "## Summary\n(one short paragraph)\n"
    "## Key Points\n(a bulleted list)\n"
    "## Action Items\n(a bulleted list of concrete next steps with an owner where "
    "stated; write '- None identified' if there are none).\n"
    "Do not invent content that is not in the transcript."
)


@runtime_checkable
class Summarizer(Protocol):
    """Turn a transcript into a markdown meeting write-up."""

    def summarize(self, transcript: str, *, title: str) -> str: ...


class LLMSummarizer:
    """Real summarizer calling a local OpenAI-compatible chat-completions LLM.

    Uses stdlib ``urllib`` (no hard httpx/requests dependency). Endpoint + model
    come from ``SKCHAT_LLM_URL`` / ``SKCHAT_LLM_MODEL``. On any failure it falls
    back to a minimal write-up built from the transcript so the pipeline still
    posts something useful.
    """

    def __init__(
        self,
        *,
        url: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._url = url or _LLM_URL
        self._model = model or _LLM_MODEL
        self._timeout = timeout

    def summarize(self, transcript: str, *, title: str) -> str:
        user = f"Title: {title}\n\nTranscript:\n{transcript}"
        body = json.dumps(
            {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": _SUMMARY_SYSTEM},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "temperature": 0.3,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"].strip()
            if content:
                return f"# {title}\n\n{content}"
            raise ValueError("empty LLM response")
        except (urllib.error.URLError, KeyError, ValueError, OSError) as exc:
            logger.warning("LLM summarization failed (%s); using fallback.", exc)
            return _fallback_writeup(transcript, title=title)


def _fallback_writeup(transcript: str, *, title: str) -> str:
    """A no-LLM write-up so a write-up always gets posted, even when the LLM is down."""
    excerpt = transcript.strip()
    if len(excerpt) > 800:
        excerpt = excerpt[:800].rstrip() + " …"
    return (
        f"# {title}\n\n"
        "## Summary\n"
        "Automated write-up unavailable (language model not reachable); raw "
        "transcript excerpt below.\n\n"
        "## Key Points\n"
        f"- {excerpt}\n\n"
        "## Action Items\n"
        "- None identified (LLM unavailable)\n"
    )


class FakeSummarizer:
    """Test double — returns a deterministic well-formed write-up, records inputs."""

    def __init__(self) -> None:
        self.seen_transcript: Optional[str] = None
        self.seen_title: Optional[str] = None

    def summarize(self, transcript: str, *, title: str) -> str:
        self.seen_transcript = transcript
        self.seen_title = title
        return (
            f"# {title}\n\n"
            "## Summary\nA fake summary.\n\n"
            "## Key Points\n- point one\n\n"
            "## Action Items\n- do the thing\n"
        )


# ===========================================================================
# Default poster: chat lane endpoint
# ===========================================================================


def chat_lane_poster(
    space_id: str,
    text: str,
    *,
    base_url: str | None = None,
    agent: str | None = None,
    timeout: float = 15.0,
) -> None:
    """POST the write-up to a Space's **chat lane** via ``/spaces/{id}/lanes/event``.

    Mirrors the Tier-2 lane envelope expected by
    :class:`skchat.spaces.lanes.LaneDispatcher`:
    ``{"lane": "chat", "from": <agent>, "text": <text>}``.
    """
    base = (base_url or _SPACES_BASE).rstrip("/")
    url = f"{base}/spaces/{space_id}/lanes/event"
    envelope = {"lane": "chat", "from": agent or _DEFAULT_AGENT, "text": text}
    data = json.dumps(envelope).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout):  # noqa: S310 (local tailnet)
        pass


# ===========================================================================
# Orchestrator
# ===========================================================================


class RecordingWriteup:
    """Transcribe a finished Space recording, write it up, post it back.

    The seams (``transcriber`` / ``summarizer`` / ``poster``) can be supplied per
    call to :meth:`process` or defaulted on the instance. Defaults are the real
    Whisper / LLM / chat-lane implementations; tests inject fakes.
    """

    def __init__(
        self,
        *,
        transcriber: Transcriber | None = None,
        summarizer: Summarizer | None = None,
        poster: Poster | None = None,
    ) -> None:
        self._transcriber = transcriber
        self._summarizer = summarizer
        self._poster = poster

    def process(
        self,
        space_id: str,
        audio_path: str,
        *,
        title: str,
        transcriber: Transcriber | None = None,
        summarizer: Summarizer | None = None,
        poster: Poster | None = None,
    ) -> str:
        """Run the full pipeline and return the text that was posted.

        Resolution order for each seam: per-call arg → instance default → the
        real implementation. A missing/empty/None transcript is handled
        gracefully: no LLM call, an honest "no audio/transcript" note is posted
        and returned.
        """
        tx = transcriber or self._transcriber or WhisperTranscriber()
        sm = summarizer or self._summarizer or LLMSummarizer()
        post = poster or self._poster or chat_lane_poster

        transcript = None
        try:
            transcript = tx.transcribe(audio_path)
        except Exception as exc:  # noqa: BLE001 — never let STT crash the pipeline
            logger.warning("Transcription raised for %s: %s", audio_path, exc)

        if not transcript or not transcript.strip():
            note = (
                f"# {title}\n\n"
                "## Summary\n"
                "No audio / no transcript was produced for this recording "
                "(silent room or transcription unavailable), so no write-up "
                "could be generated.\n"
            )
            self._post(post, space_id, note)
            return note

        writeup = sm.summarize(transcript.strip(), title=title)
        self._post(post, space_id, writeup)
        return writeup

    @staticmethod
    def _post(post: Poster, space_id: str, text: str) -> None:
        try:
            post(space_id, text)
        except Exception as exc:  # noqa: BLE001 — posting failure shouldn't lose the text
            logger.warning("Failed to post write-up for %s: %s", space_id, exc)
