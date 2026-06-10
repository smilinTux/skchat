"""Tests for VoicePlayer/VoiceRecorder pluggable-backend delegation.

The existing test_voice.py exercises the default Piper path and must stay
green; these tests verify the *new* seam: VoicePlayer/VoiceRecorder keep their
public signatures but delegate to a selectable backend (default piper/whisper),
selectable by param or env var, and that injecting a fake backend switches the
implementation.
"""

from __future__ import annotations

from skchat import voice_backends as vb
from skchat.voice import VoicePlayer, VoiceRecorder
from skchat.voice_backends import STTBackend, TTSBackend


class _FakeTTS(TTSBackend):
    name = "fake-tts"

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.stopped = False

    def is_available(self) -> bool:
        return True

    def speak(self, text, blocking=False):
        self.spoken.append(text)
        return None

    def stop(self) -> None:
        self.stopped = True


class _FakeSTT(STTBackend):
    name = "fake-stt"

    def __init__(self) -> None:
        self.records: list[int] = []

    def is_available(self) -> bool:
        return True

    def transcribe(self, audio_path, **kwargs):
        return "transcribed text"

    def record(self, duration=10):
        self.records.append(duration)
        return "transcribed text"


# ---------------------------------------------------------------------------
# Default backend = piper / whisper (public API unchanged)
# ---------------------------------------------------------------------------


def test_voiceplayer_default_backend_is_piper():
    """VoicePlayer with no backend uses the Piper backend by default."""
    player = VoicePlayer()
    assert player.backend.name == "piper"


def test_voicerecorder_default_backend_is_whisper():
    """VoiceRecorder with no backend uses the Whisper backend by default."""
    recorder = VoiceRecorder()
    assert recorder.backend.name == "whisper"


# ---------------------------------------------------------------------------
# Backend selection by name + injected instance switches implementation
# ---------------------------------------------------------------------------


def test_voiceplayer_select_backend_by_param():
    """VoicePlayer(backend='chatterbox') selects the named registered backend."""
    player = VoicePlayer(backend="chatterbox")
    assert player.backend.name == "chatterbox"


def test_voiceplayer_inject_fake_backend_switches_speak():
    """An injected backend instance receives speak() calls (impl switched)."""
    fake = _FakeTTS()
    player = VoicePlayer(backend=fake)
    player.speak("hello pluggable")
    assert fake.spoken == ["hello pluggable"]


def test_voiceplayer_is_available_delegates_to_backend():
    """is_available() reflects the selected backend's availability."""
    fake = _FakeTTS()
    player = VoicePlayer(backend=fake)
    assert player.is_available() is True


def test_voiceplayer_stop_delegates_to_backend():
    """stop() forwards to the selected backend."""
    fake = _FakeTTS()
    player = VoicePlayer(backend=fake)
    player.stop()
    assert fake.stopped is True


def test_voicerecorder_inject_fake_backend_switches_record():
    """An injected STT backend receives record() calls (impl switched)."""
    fake = _FakeSTT()
    recorder = VoiceRecorder(backend=fake)
    result = recorder.record(duration=7)
    assert result == "transcribed text"
    assert fake.records == [7]


def test_voicerecorder_available_delegates_to_backend():
    """VoiceRecorder.available reflects the selected backend's availability."""
    fake = _FakeSTT()
    recorder = VoiceRecorder(backend=fake)
    assert recorder.available is True


# ---------------------------------------------------------------------------
# Env-var selection
# ---------------------------------------------------------------------------


def test_voiceplayer_backend_from_env(monkeypatch):
    """SKCHAT_TTS_BACKEND selects the TTS backend when no param is given."""
    monkeypatch.setenv("SKCHAT_TTS_BACKEND", "chatterbox")
    player = VoicePlayer()
    assert player.backend.name == "chatterbox"


def test_voicerecorder_backend_from_env(monkeypatch):
    """SKCHAT_STT_BACKEND selects the STT backend when no param is given."""
    monkeypatch.setenv("SKCHAT_STT_BACKEND", "sensevoice")
    recorder = VoiceRecorder()
    assert recorder.backend.name == "sensevoice"


def test_voiceplayer_unknown_env_falls_back_to_default(monkeypatch):
    """An unknown SKCHAT_TTS_BACKEND degrades to the piper default, no crash."""
    monkeypatch.setenv("SKCHAT_TTS_BACKEND", "nope-not-real")
    player = VoicePlayer()
    assert player.backend.name == "piper"


# ---------------------------------------------------------------------------
# Graceful "backend unavailable" handling
# ---------------------------------------------------------------------------


def test_voiceplayer_speak_unavailable_backend_returns_none():
    """speak() returns None (no raise) when the backend is unavailable."""

    class _Unavailable(_FakeTTS):
        def is_available(self) -> bool:
            return False

        def speak(self, text, blocking=False):
            # backend itself degrades gracefully
            return None

    player = VoicePlayer(backend=_Unavailable())
    assert player.speak("nothing") is None


def test_voicerecorder_record_unavailable_backend_returns_none():
    """record() returns None when the backend is unavailable."""

    class _Unavailable(_FakeSTT):
        def is_available(self) -> bool:
            return False

        def record(self, duration=10):
            return None

    recorder = VoiceRecorder(backend=_Unavailable())
    assert recorder.available is False
    assert recorder.record() is None


def test_register_custom_backend_then_select_via_voiceplayer():
    """A newly registered backend is selectable by VoicePlayer without core edits."""
    vb.register_tts_backend("fake-tts", _FakeTTS)
    try:
        player = VoicePlayer(backend="fake-tts")
        assert player.backend.name == "fake-tts"
    finally:
        vb._TTS_BACKENDS.pop("fake-tts", None)
