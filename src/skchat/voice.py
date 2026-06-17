"""Voice playback (TTS) and recording (STT) for SKChat — pluggable backends.

``VoicePlayer`` (TTS) and ``VoiceRecorder`` (STT) keep their original public
signatures but now delegate to a **pluggable backend** selected from
:mod:`skchat.voice_backends` (ports + registry + ``get_*``).  The defaults are
the sovereign, on-device engines — **Piper** TTS and **openai-whisper** STT —
so behaviour is identical to before; other engines (e.g. the ecosystem's
Chatterbox TTS / SenseVoice STT, or any registered adapter) can be selected via
the ``backend`` parameter or the ``SKCHAT_TTS_BACKEND`` / ``SKCHAT_STT_BACKEND``
environment variables without changing call sites.

Usage::

    # TTS playback (default = piper):
    player = VoicePlayer()
    if player.is_available():
        player.speak("Hello from SKChat")
    proc = player.speak("New message", blocking=False)   # non-blocking handle
    player.stop()                                          # cancel mid-playback

    # Select a different backend by name, env, or injected instance:
    player = VoicePlayer(backend="chatterbox")
    player = VoicePlayer(backend=my_fake_tts_backend)

    # STT recording (default = whisper):
    recorder = VoiceRecorder()
    if recorder.available:
        text = recorder.record(duration=10)        # fixed-length
        text = recorder.record_interactive()       # press Enter to stop

The pluggable engines (Piper raw-PCM pipeline, Whisper transcription, the
Chatterbox/SenseVoice scaffolds) live in :mod:`skchat.voice_backends`.  For the
**default Piper** path, ``VoicePlayer`` retains the original in-class binary
discovery + playback logic (so the existing public attributes/tests are
unchanged) and only delegates to a separate backend object when a non-default
backend is selected.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Union

from .voice_backends import (
    _PIPER_SEARCH_PATHS,
    _VOICE_TMP_WAV,
    _VOICES_DIRS,
    DEFAULT_VOICE,
    LUMINA_VOICE,
    STTBackend,
    TTSBackend,
    WhisperSTTBackend,
    get_stt_backend,
    get_tts_backend,
)

logger = logging.getLogger("skchat.voice")

# Re-exported for backward compatibility (callers + existing tests import these
# names from skchat.voice). The canonical definitions live in voice_backends;
# they are aliased here so the public surface is unchanged.
__all__ = [
    "DEFAULT_VOICE",
    "LUMINA_VOICE",
    "VoicePlayer",
    "VoiceRecorder",
]

# Selection env vars.
_TTS_ENV = "SKCHAT_TTS_BACKEND"
_STT_ENV = "SKCHAT_STT_BACKEND"


class VoicePlayer:
    """Text-to-speech playback with a pluggable backend (default: Piper TTS).

    The **default Piper** engine is implemented in-class (binary discovery +
    raw-PCM ``piper → aplay`` pipeline), preserving the original public surface
    (``_piper_bin`` / ``_voice`` / ``_model_path`` / ``_current_procs``,
    ``_find_piper`` / ``_resolve_model``).  When a *non-default* backend is
    selected (by name, env var, or injected :class:`TTSBackend` instance),
    ``is_available`` / ``speak`` / ``stop`` delegate to it instead.
    """

    def __init__(
        self,
        model_path: Optional[str] = None,
        voice: str = DEFAULT_VOICE,
        backend: Optional[Union[str, TTSBackend]] = None,
    ) -> None:
        """Initialise VoicePlayer.

        Args:
            model_path: Explicit path to a ``.onnx`` voice model (Piper only).
            voice: Voice identifier used to locate the ONNX model (Piper only).
            backend: A backend name (e.g. ``"piper"``, ``"chatterbox"``), an
                injected :class:`TTSBackend` instance, or ``None`` to select
                the default (Piper, overridable via ``SKCHAT_TTS_BACKEND``).
        """
        self._voice = voice
        self._model_path = model_path
        self._piper_bin: Optional[str] = self._find_piper()
        self._current_procs: list[subprocess.Popen] = []
        self._backend: Optional[TTSBackend] = self._resolve_backend(backend)

    # ------------------------------------------------------------------
    # Backend selection
    # ------------------------------------------------------------------

    def _resolve_backend(self, backend: Optional[Union[str, TTSBackend]]) -> Optional[TTSBackend]:
        """Return a delegate backend, or ``None`` for the in-class Piper path.

        An unknown name (param or ``SKCHAT_TTS_BACKEND``) degrades to the Piper
        default so a bad selector never crashes voice playback.
        """
        if isinstance(backend, TTSBackend):
            return backend
        name = backend or os.environ.get(_TTS_ENV)
        if not name or name.lower() == "piper":
            return None  # use the in-class Piper implementation
        try:
            return get_tts_backend(name)
        except KeyError:
            logger.warning("Unknown TTS backend %r; falling back to piper.", name)
            return None

    @property
    def backend(self) -> TTSBackend:
        """The active backend.

        For the default Piper path this is the player itself (it *is* a
        TTSBackend-shaped object exposing ``name``/``is_available``/``speak``/
        ``stop``); otherwise the delegated backend instance.
        """
        return self._backend if getattr(self, "_backend", None) is not None else self

    #: Backend identifier for the in-class Piper default.
    name = "piper"

    # ------------------------------------------------------------------
    # Internal helpers (Piper default)
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
        """Return whether the active backend can synthesise speech."""
        _backend = getattr(self, "_backend", None)
        if _backend is not None:
            return _backend.is_available()
        return self._piper_bin is not None and self._resolve_model() is not None

    def speak(self, text: str, blocking: bool = False) -> Optional[subprocess.Popen]:
        """Speak *text* via the active backend.

        For the Piper default, pipes text through
        ``piper --output_raw | aplay`` (Linux) or a WAV + ``afplay`` (macOS).

        Args:
            text: The message text to synthesise.
            blocking: When ``True``, wait until playback finishes.

        Returns:
            The playback :class:`~subprocess.Popen` handle in non-blocking mode,
            ``None`` in blocking mode or when the backend is unavailable.
        """
        _backend = getattr(self, "_backend", None)
        if _backend is not None:
            return _backend.speak(text, blocking=blocking)

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
        """Stop any currently playing audio (Piper default) or delegate."""
        _backend = getattr(self, "_backend", None)
        if _backend is not None:
            _backend.stop()
            return
        for proc in self._current_procs:
            try:
                proc.terminate()
            except Exception as exc:  # noqa: BLE001
                logger.debug("failed to terminate voice proc (%s: %s)", type(exc).__name__, exc)
        self._current_procs = []


class VoiceRecorder:
    """Voice recording + transcription with a pluggable backend (default: Whisper).

    Records audio via ``arecord`` (ALSA) and transcribes through the selected
    :class:`STTBackend` — Whisper by default (local, sovereign).  Public
    signatures (``record`` / ``record_interactive`` / ``available``) are
    unchanged; transcription delegates to the backend.
    """

    def __init__(
        self,
        whisper_model: str = "base",
        backend: Optional[Union[str, STTBackend]] = None,
    ) -> None:
        """Initialise VoiceRecorder.

        Args:
            whisper_model: Whisper model size for the default backend
                (``"tiny"``/``"base"``/``"small"``/``"medium"``/``"large"``).
            backend: A backend name (e.g. ``"whisper"``, ``"sensevoice"``), an
                injected :class:`STTBackend` instance, or ``None`` for the
                default (Whisper, overridable via ``SKCHAT_STT_BACKEND``).
        """
        self.whisper_model = whisper_model
        self.backend: STTBackend = self._resolve_backend(backend, whisper_model)

    def _resolve_backend(
        self, backend: Optional[Union[str, STTBackend]], whisper_model: str
    ) -> STTBackend:
        """Resolve the STT backend (instance / name / env / default Whisper)."""
        if isinstance(backend, STTBackend):
            return backend
        name = backend or os.environ.get(_STT_ENV)
        if name and name.lower() != "whisper":
            try:
                return get_stt_backend(name)
            except KeyError:
                logger.warning("Unknown STT backend %r; falling back to whisper.", name)
        return WhisperSTTBackend(whisper_model=whisper_model)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Return whether the selected STT backend can transcribe right now."""
        return self.backend.is_available()

    def _check_available(self) -> bool:
        """Legacy helper — whether the selected backend is available."""
        return self.backend.is_available()

    def _transcribe(self, wav_path: str) -> Optional[str]:
        """Transcribe *wav_path* via the selected backend (legacy helper)."""
        if not self.available:
            return None
        return self.backend.transcribe(wav_path)

    def record(self, duration: int = 10) -> Optional[str]:
        """Record audio for *duration* seconds then transcribe.

        Args:
            duration: Recording length in seconds (default: 10).

        Returns:
            Transcribed text, or ``None`` on failure.
        """
        if not self.available:
            logger.warning(
                "STT backend unavailable. For the default backend install with: "
                "pip install openai-whisper"
            )
            return None
        return self.backend.record(duration=duration)

    def record_interactive(self) -> Optional[str]:
        """Record audio until the user presses Enter, then transcribe.

        Starts ``arecord`` in the background; on Enter it is terminated and the
        captured WAV is transcribed via the selected backend.

        Returns:
            Transcribed text, or ``None`` on failure.
        """
        if not self.available:
            logger.warning(
                "STT backend unavailable. For the default backend install with: "
                "pip install openai-whisper"
            )
            return None

        try:
            proc = subprocess.Popen(
                ["arecord", "-f", "cd", "-t", "wav", _VOICE_TMP_WAV],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            logger.warning("arecord not found; install alsa-utils (apt/pacman)")
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("arecord error: %s", exc)
            return None

        try:
            input()  # Block until the user presses Enter
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception as exc:  # noqa: BLE001
                logger.debug("failed to stop recorder proc (%s: %s)", type(exc).__name__, exc)

        return self.backend.transcribe(_VOICE_TMP_WAV)
