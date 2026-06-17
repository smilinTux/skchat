"""Tests for skchat.voice_backends — pluggable TTS/STT backend ports.

Covers the ABC contract, the registry get/unknown paths, that the default
Piper/Whisper adapters conform, that the scaffold Chatterbox/SenseVoice
adapters signal availability without requiring their binaries, and that a
fake backend can be selected and switches the implementation.

No live Piper/Whisper/Chatterbox/SenseVoice binaries are required — every
"available" probe is patched or a fake backend is injected.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from skchat import voice_backends as vb
from skchat.voice_backends import (
    STTBackend,
    TTSBackend,
    WhisperSTTBackend,
    get_stt_backend,
    get_tts_backend,
    list_stt_backends,
    list_tts_backends,
    register_stt_backend,
    register_tts_backend,
)

# ---------------------------------------------------------------------------
# ABC conformance
# ---------------------------------------------------------------------------


def test_tts_backend_is_abstract():
    """TTSBackend cannot be instantiated directly (it is an ABC)."""
    with pytest.raises(TypeError):
        TTSBackend()  # type: ignore[abstract]


def test_stt_backend_is_abstract():
    """STTBackend cannot be instantiated directly (it is an ABC)."""
    with pytest.raises(TypeError):
        STTBackend()  # type: ignore[abstract]


def test_incomplete_tts_subclass_cannot_instantiate():
    """A TTS subclass missing required methods stays abstract."""

    class Partial(TTSBackend):
        name = "partial"

        # is_available implemented, speak/stop missing
        def is_available(self) -> bool:  # pragma: no cover - never reached
            return True

    with pytest.raises(TypeError):
        Partial()  # type: ignore[abstract]


def test_piper_backend_conforms_to_tts_abc():
    """The default Piper adapter is a concrete TTSBackend."""
    from skchat.voice_backends import PiperTTSBackend

    assert issubclass(PiperTTSBackend, TTSBackend)
    backend = PiperTTSBackend()
    assert isinstance(backend, TTSBackend)
    assert backend.name == "piper"


def test_whisper_backend_conforms_to_stt_abc():
    """The default Whisper adapter is a concrete STTBackend."""
    from skchat.voice_backends import WhisperSTTBackend

    assert issubclass(WhisperSTTBackend, STTBackend)
    backend = WhisperSTTBackend()
    assert isinstance(backend, STTBackend)
    assert backend.name == "whisper"


# ---------------------------------------------------------------------------
# Registry: get / defaults / unknown
# ---------------------------------------------------------------------------


def test_get_tts_backend_default_is_piper():
    """get_tts_backend() with no name returns the Piper default."""
    backend = get_tts_backend()
    assert backend.name == "piper"


def test_get_stt_backend_default_is_whisper():
    """get_stt_backend() with no name returns the Whisper default."""
    backend = get_stt_backend()
    assert backend.name == "whisper"


def test_get_tts_backend_by_name():
    """get_tts_backend('piper') returns a Piper backend instance."""
    from skchat.voice_backends import PiperTTSBackend

    assert isinstance(get_tts_backend("piper"), PiperTTSBackend)


def test_get_stt_backend_by_name():
    """get_stt_backend('whisper') returns a Whisper backend instance."""
    from skchat.voice_backends import WhisperSTTBackend

    assert isinstance(get_stt_backend("whisper"), WhisperSTTBackend)


def test_get_tts_backend_case_insensitive():
    """Backend names resolve case-insensitively."""
    assert get_tts_backend("PIPER").name == "piper"


def test_get_tts_backend_unknown_raises():
    """An unknown TTS backend name raises a clear error."""
    with pytest.raises(KeyError):
        get_tts_backend("does-not-exist")


def test_get_stt_backend_unknown_raises():
    """An unknown STT backend name raises a clear error."""
    with pytest.raises(KeyError):
        get_stt_backend("does-not-exist")


def test_scaffold_backends_registered():
    """Chatterbox (TTS) and SenseVoice (STT) scaffolds are registered."""
    assert "chatterbox" in list_tts_backends()
    assert "sensevoice" in list_stt_backends()
    assert "piper" in list_tts_backends()
    assert "whisper" in list_stt_backends()


# ---------------------------------------------------------------------------
# Scaffold adapters: available iff dependency present (NOT required)
# ---------------------------------------------------------------------------


def test_chatterbox_unavailable_without_dep():
    """Chatterbox TTS scaffold reports unavailable when its dep is absent."""
    from skchat.voice_backends import ChatterboxTTSBackend

    backend = ChatterboxTTSBackend()
    with patch.object(backend, "_probe", return_value=False):
        assert backend.is_available() is False
        # speak() degrades gracefully — returns None, never raises
        assert backend.speak("hello") is None


def test_chatterbox_available_when_dep_present():
    """Chatterbox reports available when its probe succeeds (dep present)."""
    from skchat.voice_backends import ChatterboxTTSBackend

    backend = ChatterboxTTSBackend()
    with patch.object(backend, "_probe", return_value=True):
        assert backend.is_available() is True


def test_sensevoice_unavailable_without_dep():
    """SenseVoice STT scaffold reports unavailable when its dep is absent."""
    from skchat.voice_backends import SenseVoiceSTTBackend

    backend = SenseVoiceSTTBackend()
    with patch.object(backend, "_probe", return_value=False):
        assert backend.is_available() is False
        # transcribe() degrades gracefully — returns None
        assert backend.transcribe("/tmp/none.wav") is None


def test_sensevoice_available_when_dep_present():
    """SenseVoice reports available when its probe succeeds."""
    from skchat.voice_backends import SenseVoiceSTTBackend

    backend = SenseVoiceSTTBackend()
    with patch.object(backend, "_probe", return_value=True):
        assert backend.is_available() is True


# ---------------------------------------------------------------------------
# Custom registration + selection switches implementation (fake backend)
# ---------------------------------------------------------------------------


class _FakeTTS(TTSBackend):
    """A fake TTS backend that records calls instead of producing audio."""

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
    """A fake STT backend that returns a canned transcript."""

    name = "fake-stt"

    def __init__(self) -> None:
        self.transcribed: list[str] = []

    def is_available(self) -> bool:
        return True

    def transcribe(self, audio_path, **kwargs):
        self.transcribed.append(audio_path)
        return "canned transcript"

    def record(self, duration=10):  # pragma: no cover - exercised via voice.py
        return self.transcribe("recorded")


def test_register_and_get_custom_tts_backend():
    """A backend can register without editing core, then be fetched by name."""
    register_tts_backend("fake-tts", _FakeTTS)
    try:
        backend = get_tts_backend("fake-tts")
        assert isinstance(backend, _FakeTTS)
        backend.speak("hello")
        assert backend.spoken == ["hello"]
    finally:
        vb._TTS_BACKENDS.pop("fake-tts", None)


def test_register_and_get_custom_stt_backend():
    """An STT backend registers via the open seam and resolves by name."""
    register_stt_backend("fake-stt", _FakeSTT)
    try:
        backend = get_stt_backend("fake-stt")
        assert isinstance(backend, _FakeSTT)
        assert backend.transcribe("/tmp/x.wav") == "canned transcript"
    finally:
        vb._STT_BACKENDS.pop("fake-stt", None)


def test_register_rejects_non_subclass():
    """Registering a class that is not a TTSBackend subclass raises."""

    class NotABackend:
        pass

    with pytest.raises(TypeError):
        register_tts_backend("bogus", NotABackend)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Default Piper/Whisper adapters keep behaving like the originals
# ---------------------------------------------------------------------------


def test_piper_backend_is_available_reflects_binary_and_model(tmp_path):
    """PiperTTSBackend.is_available mirrors VoicePlayer's binary+model check."""
    from skchat.voice_backends import PiperTTSBackend

    model = tmp_path / "en_US-lessac-medium.onnx"
    model.touch()
    with patch("shutil.which", return_value="/usr/local/bin/piper"):
        backend = PiperTTSBackend(model_path=str(model))
    assert backend.is_available() is True


def test_piper_backend_speak_degrades_when_unavailable():
    """PiperTTSBackend.speak returns None when piper is unavailable."""
    from skchat.voice_backends import PiperTTSBackend

    with patch("shutil.which", return_value=None):
        backend = PiperTTSBackend(model_path="/nonexistent/model.onnx")
    assert backend.speak("hello") is None


def test_whisper_backend_unavailable_without_package():
    """WhisperSTTBackend.is_available is False when openai-whisper is absent."""
    from skchat.voice_backends import WhisperSTTBackend

    backend = WhisperSTTBackend()
    with patch.object(backend, "_probe", return_value=False):
        assert backend.is_available() is False
        assert backend.transcribe("/tmp/x.wav") is None


def test_whisper_backend_transcribe_uses_whisper_api():
    """WhisperSTTBackend.transcribe loads the model and returns stripped text."""
    from skchat.voice_backends import WhisperSTTBackend

    fake_whisper = MagicMock()
    fake_model = MagicMock()
    fake_model.transcribe.return_value = {"text": "  hello world  "}
    fake_whisper.load_model.return_value = fake_model

    backend = WhisperSTTBackend(whisper_model="base")
    with (
        patch.object(backend, "_probe", return_value=True),
        patch.dict("sys.modules", {"whisper": fake_whisper}),
    ):
        result = backend.transcribe("/tmp/audio.wav")

    assert result == "hello world"
    fake_whisper.load_model.assert_called_once_with("base")


def test_stt_record_returns_none_when_unavailable():
    """STTBackend.record short-circuits to None when the engine is unavailable."""
    backend = WhisperSTTBackend()
    with patch.object(backend, "is_available", return_value=False):
        assert backend.record(duration=1) is None


def test_stt_record_returns_none_when_arecord_fails():
    """record() returns None if the ALSA capture (_arecord) fails, not raising."""
    backend = WhisperSTTBackend()
    with (
        patch.object(backend, "is_available", return_value=True),
        patch.object(vb, "_arecord", return_value=False),
    ):
        assert backend.record(duration=1) is None


def test_stt_record_transcribes_after_successful_capture():
    """A successful arecord feeds the temp WAV into transcribe()."""
    backend = WhisperSTTBackend()
    with (
        patch.object(backend, "is_available", return_value=True),
        patch.object(vb, "_arecord", return_value=True),
        patch.object(backend, "transcribe", return_value="hi there") as tr,
    ):
        result = backend.record(duration=2)
    assert result == "hi there"
    tr.assert_called_once_with(vb._VOICE_TMP_WAV)


def test_arecord_missing_binary_returns_false():
    """_arecord degrades to False (never raises) when arecord isn't installed."""
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert vb._arecord("/tmp/x.wav", 1) is False


def test_arecord_nonzero_exit_returns_false():
    """_arecord returns False when arecord exits non-zero."""
    fake = MagicMock()
    fake.returncode = 1
    fake.stderr = b"device busy"
    with patch("subprocess.run", return_value=fake):
        assert vb._arecord("/tmp/x.wav", 1) is False


def test_whisper_transcribe_empty_text_returns_none():
    """An empty/whitespace transcript collapses to None (no empty messages)."""
    fake_whisper = MagicMock()
    fake_model = MagicMock()
    fake_model.transcribe.return_value = {"text": "   "}
    fake_whisper.load_model.return_value = fake_model
    backend = WhisperSTTBackend()
    with (
        patch.object(backend, "_probe", return_value=True),
        patch.dict("sys.modules", {"whisper": fake_whisper}),
    ):
        assert backend.transcribe("/tmp/a.wav") is None


def test_piper_backend_speak_launches_pipeline():
    """PiperTTSBackend.speak spawns piper→aplay and returns the aplay handle."""
    from skchat.voice_backends import PiperTTSBackend

    piper_proc = MagicMock(spec=subprocess.Popen)
    piper_proc.stdin = MagicMock()
    piper_proc.stdout = MagicMock()
    aplay_proc = MagicMock(spec=subprocess.Popen)

    backend = PiperTTSBackend()
    backend._piper_bin = "/usr/local/bin/piper"
    backend._resolve_model = lambda: "/fake/model.onnx"  # type: ignore[method-assign]

    with patch("subprocess.Popen", side_effect=[piper_proc, aplay_proc]):
        result = backend.speak("hi", blocking=False)

    assert result is aplay_proc
    piper_proc.stdin.write.assert_called_once_with(b"hi")
