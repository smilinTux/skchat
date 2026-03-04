"""Piper TTS voice playback and Whisper STT recording for SKChat.

Provides sovereign, local text-to-speech via Piper TTS and
speech-to-text via openai-whisper.  No cloud dependency: both run
entirely on-device.

Usage::

    # TTS playback:
    player = VoicePlayer()
    if player.is_available():
        player.speak("Hello from SKChat")

    # Non-blocking — returns Popen handle for the aplay process:
    proc = player.speak("New message", blocking=False)

    # Stop mid-playback:
    player.stop()

    # STT recording:
    recorder = VoiceRecorder()
    if recorder.available:
        text = recorder.record(duration=10)        # fixed-length
        text = recorder.record_interactive()       # press Enter to stop
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skchat.voice")

# Piper binary search order (shutil.which is checked first).
_PIPER_SEARCH_PATHS: list[str] = [
    str(Path.home() / ".local/bin/piper"),
    "/usr/local/bin/piper",
    "/usr/bin/piper",
]

# Default location for downloaded voice models.
_VOICES_DIR: Path = Path.home() / ".local/share/piper/voices"

# Well-known voice names.
DEFAULT_VOICE = "en_US-lessac-medium"
LUMINA_VOICE = "en_US-jenny-medium"


class VoicePlayer:
    """Text-to-speech playback via Piper TTS (local, sovereign).

    Locates the ``piper`` binary and an ONNX voice model on construction.
    All playback is piped through ``aplay`` as raw PCM, keeping the
    dependency surface minimal (no Python audio library needed).
    """

    def __init__(
        self,
        model_path: Optional[str] = None,
        voice: str = DEFAULT_VOICE,
    ) -> None:
        """Initialise VoicePlayer.

        Args:
            model_path: Explicit path to a ``.onnx`` voice model.  When
                ``None`` the standard ``~/.local/share/piper/voices/``
                directory is searched by *voice* name.
            voice: Voice identifier used to locate the ONNX model, e.g.
                ``"en_US-lessac-medium"``.  Ignored when *model_path* is
                given.
        """
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
        model_file = _VOICES_DIR / f"{self._voice}.onnx"
        if model_file.exists():
            return str(model_file)
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return ``True`` if piper binary and voice model are both present."""
        return self._piper_bin is not None and self._resolve_model() is not None

    def speak(
        self, text: str, blocking: bool = False
    ) -> Optional[subprocess.Popen]:
        """Speak *text* via Piper TTS.

        Pipes text through::

            piper --model <model> --output_raw | aplay -r 22050 -f S16_LE -t raw -

        Args:
            text: The message text to synthesise.
            blocking: When ``True``, wait until playback finishes before
                returning.  When ``False`` (default) return immediately with
                the ``aplay`` :class:`subprocess.Popen` handle so the caller
                can wait or cancel later.

        Returns:
            The ``aplay`` :class:`~subprocess.Popen` handle in non-blocking
            mode, or ``None`` in blocking mode (or when Piper is unavailable).
        """
        if not self.is_available():
            logger.warning(
                "Piper TTS not available (binary=%s, model=%s); skipping speech.",
                self._piper_bin,
                self._resolve_model(),
            )
            return None

        model = self._resolve_model()

        try:
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
            except Exception:  # noqa: BLE001
                pass
        self._current_procs = []


# ---------------------------------------------------------------------------
# Temporary file paths for voice recording
# ---------------------------------------------------------------------------

_VOICE_TMP_WAV: str = "/tmp/skchat-voice.wav"


class VoiceRecorder:
    """Voice message recording via arecord + Whisper STT transcription.

    Records audio using ``arecord`` (ALSA) and transcribes locally with
    the ``openai-whisper`` Python package.  No cloud dependency.

    Usage::

        recorder = VoiceRecorder()
        if recorder.available:
            # Fixed-duration recording:
            text = recorder.record(duration=10)

            # Interactive — press Enter to stop:
            text = recorder.record_interactive()
    """

    def __init__(self, whisper_model: str = "base") -> None:
        """Initialise VoiceRecorder.

        Args:
            whisper_model: Whisper model size to load for transcription.
                Common values: ``"tiny"``, ``"base"``, ``"small"``,
                ``"medium"``, ``"large"``.  Larger models are more accurate
                but slower to load.
        """
        self.whisper_model = whisper_model
        self.available = self._check_available()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_available(self) -> bool:
        """Return ``True`` if the ``openai-whisper`` package is importable."""
        try:
            import whisper  # noqa: F401 — openai-whisper package
            return True
        except ImportError:
            logger.warning(
                "openai-whisper not installed; voice recording unavailable. "
                "Install with: pip install openai-whisper"
            )
            return False

    def _run_arecord(self, wav_path: str, duration: int) -> bool:
        """Record audio to *wav_path* for *duration* seconds via ``arecord``.

        Args:
            wav_path: Output WAV file path.
            duration: Recording duration in seconds.

        Returns:
            ``True`` on success, ``False`` if ``arecord`` is unavailable or
            the process failed.
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

    def _transcribe(self, wav_path: str) -> Optional[str]:
        """Transcribe *wav_path* using the Whisper Python API.

        Args:
            wav_path: Path to a WAV audio file.

        Returns:
            Stripped transcription text, or ``None`` on failure / empty result.
        """
        if not self.available:
            return None
        try:
            import whisper

            model = whisper.load_model(self.whisper_model)
            result = model.transcribe(wav_path)
            text: str = result.get("text", "").strip()
            return text if text else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Whisper transcription error: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, duration: int = 10) -> Optional[str]:
        """Record audio for *duration* seconds then transcribe with Whisper.

        Args:
            duration: Recording length in seconds (default: 10).

        Returns:
            Transcribed text, or ``None`` on failure.
        """
        if not self.available:
            logger.warning(
                "openai-whisper not available. "
                "Install with: pip install openai-whisper"
            )
            return None

        if not self._run_arecord(_VOICE_TMP_WAV, duration):
            return None

        return self._transcribe(_VOICE_TMP_WAV)

    def record_interactive(self) -> Optional[str]:
        """Record audio until the user presses Enter, then transcribe.

        Starts ``arecord`` in the background.  When the user presses Enter,
        ``SIGTERM`` is sent to the recording process and the captured WAV is
        transcribed with Whisper.

        Returns:
            Transcribed text, or ``None`` on failure.
        """
        if not self.available:
            logger.warning(
                "openai-whisper not available. "
                "Install with: pip install openai-whisper"
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
            except Exception:  # noqa: BLE001
                pass

        return self._transcribe(_VOICE_TMP_WAV)
