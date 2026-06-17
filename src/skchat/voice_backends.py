"""Pluggable TTS/STT voice backends for SKChat (ports + registry + get_*).

Two clean interfaces every voice engine plugs into — mirroring the ports/
adapters + registry + ``get_*`` shape used elsewhere in the ecosystem
(``skops.itsm`` adapters, ``skcomms`` transports):

    TTSBackend.speak(text, blocking)  -> Optional[Popen]   (text-to-speech)
    STTBackend.transcribe(audio_path) -> Optional[str]     (speech-to-text)
    STTBackend.record(duration)       -> Optional[str]     (capture + transcribe)

The **default** adapters are :class:`PiperTTSBackend` (Piper TTS) and
:class:`WhisperSTTBackend` (openai-whisper STT) — they keep skchat's existing,
sovereign, on-device behaviour exactly (no cloud dependency).  Other engines
register through the open seam (:func:`register_tts_backend` /
:func:`register_stt_backend`) without editing core; the ecosystem's
**Chatterbox TTS** and **SenseVoice STT** ship here as thin scaffold adapters
that signal "available iff <dep> present" and never require their binaries.

Design rule (matches skops ports): every adapter is standalone-testable —
availability probes are patchable and ``speak``/``transcribe`` degrade
gracefully (return ``None``, never raise) when the engine is unavailable, so a
missing binary never crashes the caller.  Selection: ``get_tts_backend(name)``
/ ``get_stt_backend(name)`` (name defaults to ``piper`` / ``whisper``; unknown
names raise ``KeyError``).
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skchat.voice_backends")

# Piper binary search order (shutil.which is checked first).
_PIPER_SEARCH_PATHS: list[str] = [
    str(Path.home() / ".local/bin/piper"),
    "/usr/local/bin/piper",
    "/usr/bin/piper",
]

# Default location for downloaded voice models.
_VOICES_DIRS: list[Path] = [Path.home() / ".local/share/piper/voices"]
if platform.system() == "Darwin":
    _VOICES_DIRS.insert(0, Path.home() / "Library/Application Support/piper/voices")

# Well-known voice names.
DEFAULT_VOICE = "en_US-lessac-medium"
LUMINA_VOICE = "en_US-jenny-medium"

# Temporary WAV path used by recording backends.
_VOICE_TMP_WAV: str = os.path.join(tempfile.gettempdir(), "skchat-voice.wav")

#: Default backend names (selected when no name / env override is given).
DEFAULT_TTS_BACKEND = "piper"
DEFAULT_STT_BACKEND = "whisper"


# ===========================================================================
# Ports
# ===========================================================================


class TTSBackend(ABC):
    """Text-to-speech port. Each engine is an adapter implementing speak()."""

    #: Adapter identifier (the registry key, case-insensitive lookup).
    name: str = "tts"

    @abstractmethod
    def is_available(self) -> bool:
        """Return whether this backend can synthesise speech right now."""
        raise NotImplementedError

    @abstractmethod
    def speak(self, text: str, blocking: bool = False) -> Optional[subprocess.Popen]:
        """Speak *text*. Return a Popen handle (non-blocking) or None.

        Must degrade gracefully — when the engine is unavailable, return
        ``None`` rather than raising, so callers never crash on a missing dep.
        """
        raise NotImplementedError

    def stop(self) -> None:
        """Stop any in-flight playback. Default: no-op (override if stateful)."""


class STTBackend(ABC):
    """Speech-to-text port. Each engine is an adapter implementing transcribe()."""

    #: Adapter identifier (the registry key, case-insensitive lookup).
    name: str = "stt"

    @abstractmethod
    def is_available(self) -> bool:
        """Return whether this backend can transcribe audio right now."""
        raise NotImplementedError

    @abstractmethod
    def transcribe(self, audio_path: str, **kwargs) -> Optional[str]:
        """Transcribe the audio file at *audio_path*; None on failure.

        Must degrade gracefully (return ``None``, never raise) when the engine
        is unavailable or transcription fails.
        """
        raise NotImplementedError

    def record(self, duration: int = 10) -> Optional[str]:
        """Capture *duration* seconds of audio then transcribe it.

        Default implementation records via ``arecord`` (ALSA) to a temp WAV
        and delegates to :meth:`transcribe`.  Engines with their own capture
        path may override.
        """
        if not self.is_available():
            return None
        if not _arecord(_VOICE_TMP_WAV, duration):
            return None
        return self.transcribe(_VOICE_TMP_WAV)


# ===========================================================================
# Shared ALSA capture helper
# ===========================================================================


def _arecord(wav_path: str, duration: int) -> bool:
    """Record audio to *wav_path* for *duration* seconds via ``arecord``.

    Returns ``True`` on success, ``False`` if ``arecord`` is unavailable or
    the process failed.  Never raises.
    """
    try:
        result = subprocess.run(
            ["arecord", "-d", str(duration), "-f", "cd", "-t", "wav", wav_path],
            capture_output=True,
        )
        if result.returncode != 0:
            logger.warning(
                "arecord exited with code %d: %s",
                result.returncode,
                result.stderr.decode(errors="replace").strip(),
            )
            return False
        return True
    except FileNotFoundError:
        logger.warning("arecord not found; install alsa-utils (apt/pacman)")
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("arecord error: %s", exc)
        return False


# ===========================================================================
# Default adapter: Piper TTS (sovereign, on-device)
# ===========================================================================


class PiperTTSBackend(TTSBackend):
    """Text-to-speech via Piper TTS (local, sovereign) — the default adapter.

    Locates the ``piper`` binary and an ONNX voice model, then pipes raw PCM
    through ``aplay`` (Linux) or a WAV file + ``afplay`` (macOS).  Keeps the
    exact behaviour of the original ``VoicePlayer`` so existing tests stay
    green.
    """

    name = "piper"

    def __init__(
        self,
        model_path: Optional[str] = None,
        voice: str = DEFAULT_VOICE,
    ) -> None:
        self._voice = voice
        self._model_path = model_path
        self._piper_bin: Optional[str] = self._find_piper()
        self._current_procs: list[subprocess.Popen] = []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_piper(self) -> Optional[str]:
        """Return the path to the piper binary, or ``None`` if absent."""
        found = shutil.which("piper")
        if found:
            return found
        for candidate in _PIPER_SEARCH_PATHS:
            if Path(candidate).exists():
                return candidate
        return None

    def _resolve_model(self) -> Optional[str]:
        """Return the resolved ONNX model path, or ``None`` if not found."""
        if self._model_path:
            p = Path(self._model_path)
            return str(p) if p.exists() else None
        for voices_dir in _VOICES_DIRS:
            model_file = voices_dir / f"{self._voice}.onnx"
            if model_file.exists():
                return str(model_file)
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return ``True`` if piper binary and voice model are both present."""
        return self._piper_bin is not None and self._resolve_model() is not None

    def speak(self, text: str, blocking: bool = False) -> Optional[subprocess.Popen]:
        """Speak *text* via Piper TTS (see module docstring for the pipeline)."""
        if not self.is_available():
            logger.warning(
                "Piper TTS not available (binary=%s, model=%s); skipping speech.",
                self._piper_bin,
                self._resolve_model(),
            )
            return None

        model = self._resolve_model()

        try:
            if platform.system() == "Darwin":
                # macOS: afplay cannot read from a pipe, so synthesise to a
                # WAV file first, then play it.
                wav_path = os.path.join(tempfile.gettempdir(), "skchat-tts.wav")
                piper_proc = subprocess.Popen(
                    [self._piper_bin, "--model", model, "--output_file", wav_path],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                assert piper_proc.stdin is not None
                piper_proc.stdin.write(text.encode("utf-8"))
                piper_proc.stdin.close()
                piper_proc.wait()

                play_proc = subprocess.Popen(
                    ["afplay", wav_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._current_procs = [play_proc]

                if blocking:
                    play_proc.wait()
                    return None
                return play_proc
            else:
                # Linux: pipe raw PCM from piper directly into aplay.
                piper_proc = subprocess.Popen(
                    [self._piper_bin, "--model", model, "--output_raw"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )

                aplay_proc = subprocess.Popen(
                    ["aplay", "-r", "22050", "-f", "S16_LE", "-t", "raw", "-"],
                    stdin=piper_proc.stdout,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

                # Close parent's copy of the pipe so aplay sees EOF when piper exits.
                assert piper_proc.stdout is not None
                piper_proc.stdout.close()

                # Feed the text to piper then signal EOF.
                assert piper_proc.stdin is not None
                piper_proc.stdin.write(text.encode("utf-8"))
                piper_proc.stdin.close()

                self._current_procs = [piper_proc, aplay_proc]

                if blocking:
                    piper_proc.wait()
                    aplay_proc.wait()
                    return None

                return aplay_proc

        except FileNotFoundError as exc:
            logger.warning("Piper TTS playback failed (command not found): %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Piper TTS error: %s", exc)
            return None

    def stop(self) -> None:
        """Terminate any currently playing audio processes."""
        for proc in self._current_procs:
            try:
                proc.terminate()
            except Exception as exc:  # noqa: BLE001
                logger.debug("failed to terminate audio proc (%s: %s)", type(exc).__name__, exc)
        self._current_procs = []


# ===========================================================================
# Default adapter: Whisper STT (sovereign, on-device)
# ===========================================================================


class WhisperSTTBackend(STTBackend):
    """Speech-to-text via openai-whisper (local, sovereign) — the default."""

    name = "whisper"

    def __init__(self, whisper_model: str = "base") -> None:
        self.whisper_model = whisper_model

    def _probe(self) -> bool:
        """Return ``True`` if the ``openai-whisper`` package is importable."""
        try:
            import whisper  # noqa: F401 — openai-whisper package

            return True
        except ImportError:
            return False

    def is_available(self) -> bool:
        """Return ``True`` if openai-whisper is installed."""
        if not self._probe():
            logger.warning(
                "openai-whisper not installed; STT unavailable. "
                "Install with: pip install openai-whisper"
            )
            return False
        return True

    def transcribe(self, audio_path: str, **kwargs) -> Optional[str]:
        """Transcribe *audio_path* with the Whisper Python API."""
        if not self._probe():
            return None
        model_name = kwargs.get("whisper_model", self.whisper_model)
        try:
            import whisper

            model = whisper.load_model(model_name)
            result = model.transcribe(audio_path)
            text: str = result.get("text", "").strip()
            return text if text else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Whisper transcription error: %s", exc)
            return None


# ===========================================================================
# Scaffold adapters: Chatterbox TTS + SenseVoice STT
# (conform to the ABC; available iff dep present; NOT required)
# ===========================================================================


class ChatterboxTTSBackend(TTSBackend):
    """Scaffold adapter for Chatterbox TTS (ecosystem engine, optional).

    Conforms to :class:`TTSBackend` but does not bundle the engine.  It reports
    available **iff** the ``chatterbox`` package is importable, and degrades
    gracefully (``speak`` → ``None``) when it is not — so selecting it never
    crashes and the Piper default remains the only hard-wired engine.
    """

    name = "chatterbox"

    def __init__(self, voice: str = DEFAULT_VOICE) -> None:
        self._voice = voice

    def _probe(self) -> bool:
        """Return ``True`` if the optional ``chatterbox`` dependency is present."""
        import importlib.util

        return importlib.util.find_spec("chatterbox") is not None

    def is_available(self) -> bool:
        return self._probe()

    def speak(self, text: str, blocking: bool = False) -> Optional[subprocess.Popen]:
        if not self.is_available():
            logger.warning("Chatterbox TTS not available; install the engine to enable it.")
            return None
        # Real synthesis is wired when the engine lands; scaffold degrades.
        logger.info("Chatterbox TTS scaffold: would synthesise %d chars.", len(text))
        return None


class SenseVoiceSTTBackend(STTBackend):
    """Scaffold adapter for SenseVoice STT (ecosystem engine, optional).

    Conforms to :class:`STTBackend` but does not bundle the engine.  It reports
    available **iff** the ``funasr`` package (SenseVoice's runtime) is
    importable, and degrades gracefully (``transcribe`` → ``None``) when it is
    not, so Whisper remains the only hard-wired STT engine.
    """

    name = "sensevoice"

    def __init__(self, model: str = "iic/SenseVoiceSmall") -> None:
        self._model = model

    def _probe(self) -> bool:
        """Return ``True`` if SenseVoice's optional runtime (``funasr``) is present."""
        import importlib.util

        return importlib.util.find_spec("funasr") is not None

    def is_available(self) -> bool:
        return self._probe()

    def transcribe(self, audio_path: str, **kwargs) -> Optional[str]:
        if not self.is_available():
            logger.warning("SenseVoice STT not available; install funasr to enable it.")
            return None
        # Real transcription is wired when the engine lands; scaffold degrades.
        logger.info("SenseVoice STT scaffold: would transcribe %s.", audio_path)
        return None


# ===========================================================================
# Registry + get_* (open seam — register without editing core)
# ===========================================================================

_TTS_BACKENDS: dict[str, type[TTSBackend]] = {
    "piper": PiperTTSBackend,
    "chatterbox": ChatterboxTTSBackend,
}

_STT_BACKENDS: dict[str, type[STTBackend]] = {
    "whisper": WhisperSTTBackend,
    "sensevoice": SenseVoiceSTTBackend,
}


def register_tts_backend(name: str, cls: type[TTSBackend]) -> None:
    """Register a TTS backend under *name* (open seam; no core edit needed).

    Args:
        name: Registry key (stored lower-case).
        cls: A concrete :class:`TTSBackend` subclass.

    Raises:
        TypeError: If *cls* is not a TTSBackend subclass.
    """
    if not (isinstance(cls, type) and issubclass(cls, TTSBackend)):
        raise TypeError(f"{cls!r} is not a TTSBackend subclass")
    _TTS_BACKENDS[name.lower()] = cls


def register_stt_backend(name: str, cls: type[STTBackend]) -> None:
    """Register an STT backend under *name* (open seam; no core edit needed).

    Args:
        name: Registry key (stored lower-case).
        cls: A concrete :class:`STTBackend` subclass.

    Raises:
        TypeError: If *cls* is not an STTBackend subclass.
    """
    if not (isinstance(cls, type) and issubclass(cls, STTBackend)):
        raise TypeError(f"{cls!r} is not an STTBackend subclass")
    _STT_BACKENDS[name.lower()] = cls


def get_tts_backend(name: Optional[str] = None, **kwargs) -> TTSBackend:
    """Return a TTS backend instance for *name* (default: piper).

    Args:
        name: Backend name (case-insensitive).  ``None`` selects the default.
        **kwargs: Forwarded to the backend constructor (e.g. ``voice=...``).

    Raises:
        KeyError: If *name* is not a registered backend.
    """
    key = (name or DEFAULT_TTS_BACKEND).lower()
    try:
        cls = _TTS_BACKENDS[key]
    except KeyError:
        raise KeyError(
            f"Unknown TTS backend {name!r}; registered: {sorted(_TTS_BACKENDS)}"
        ) from None
    return cls(**kwargs)


def get_stt_backend(name: Optional[str] = None, **kwargs) -> STTBackend:
    """Return an STT backend instance for *name* (default: whisper).

    Args:
        name: Backend name (case-insensitive).  ``None`` selects the default.
        **kwargs: Forwarded to the backend constructor (e.g. ``whisper_model=...``).

    Raises:
        KeyError: If *name* is not a registered backend.
    """
    key = (name or DEFAULT_STT_BACKEND).lower()
    try:
        cls = _STT_BACKENDS[key]
    except KeyError:
        raise KeyError(
            f"Unknown STT backend {name!r}; registered: {sorted(_STT_BACKENDS)}"
        ) from None
    return cls(**kwargs)


def list_tts_backends() -> list[str]:
    """Return the registered TTS backend names, sorted."""
    return sorted(_TTS_BACKENDS)


def list_stt_backends() -> list[str]:
    """Return the registered STT backend names, sorted."""
    return sorted(_STT_BACKENDS)
