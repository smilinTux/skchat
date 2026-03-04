"""Tests for skchat.voice — Piper TTS playback.

All subprocess calls are mocked so no actual audio is produced in CI.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from skchat.voice import DEFAULT_VOICE, LUMINA_VOICE, VoicePlayer


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _make_player(binary: str = "/usr/local/bin/piper", model: str = "/fake/model.onnx") -> VoicePlayer:
    """Return a VoicePlayer with mocked binary/model discovery."""
    player = VoicePlayer.__new__(VoicePlayer)
    player._voice = DEFAULT_VOICE
    player._model_path = None
    player._piper_bin = binary
    player._current_procs = []

    # Patch _resolve_model to return a fixed path.
    player._resolve_model = lambda: model  # type: ignore[method-assign]
    return player


def _mock_popen(returncode: int = 0) -> MagicMock:
    """Return a MagicMock that looks like a completed Popen handle."""
    proc = MagicMock(spec=subprocess.Popen)
    proc.returncode = returncode
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    proc.wait.return_value = returncode
    return proc


# ──────────────────────────────────────────────────────────────────────────────
# is_available
# ──────────────────────────────────────────────────────────────────────────────


def test_is_available_true_when_binary_and_model_exist(tmp_path: Path) -> None:
    """is_available() returns True when piper binary and model file are present."""
    model = tmp_path / "en_US-lessac-medium.onnx"
    model.touch()

    with patch("shutil.which", return_value="/usr/local/bin/piper"):
        player = VoicePlayer(model_path=str(model))

    assert player.is_available() is True


def test_is_available_false_when_binary_missing(tmp_path: Path) -> None:
    """is_available() returns False when piper binary cannot be found."""
    model = tmp_path / "en_US-lessac-medium.onnx"
    model.touch()

    with patch("shutil.which", return_value=None):
        player = VoicePlayer(model_path=str(model))

    assert player.is_available() is False


def test_is_available_false_when_model_missing() -> None:
    """is_available() returns False when the voice model file does not exist."""
    with patch("shutil.which", return_value="/usr/local/bin/piper"):
        player = VoicePlayer(model_path="/nonexistent/model.onnx")

    assert player.is_available() is False


def test_is_available_false_when_both_missing() -> None:
    """is_available() returns False when neither binary nor model are present."""
    with patch("shutil.which", return_value=None):
        player = VoicePlayer(model_path="/nonexistent/model.onnx")

    assert player.is_available() is False


# ──────────────────────────────────────────────────────────────────────────────
# speak — graceful degradation
# ──────────────────────────────────────────────────────────────────────────────


def test_speak_returns_none_when_not_available() -> None:
    """speak() returns None and does not raise when Piper is unavailable."""
    with patch("shutil.which", return_value=None):
        player = VoicePlayer(model_path="/nonexistent/model.onnx")

    result = player.speak("Hello world")
    assert result is None


# ──────────────────────────────────────────────────────────────────────────────
# speak — subprocess interaction
# ──────────────────────────────────────────────────────────────────────────────


def test_speak_nonblocking_launches_pipeline() -> None:
    """speak() spawns a piper→aplay pipeline and returns the aplay Popen handle."""
    piper_proc = _mock_popen()
    aplay_proc = _mock_popen()

    player = _make_player()

    with patch("subprocess.Popen", side_effect=[piper_proc, aplay_proc]) as mock_popen:
        result = player.speak("Test message", blocking=False)

    assert result is aplay_proc

    # Verify piper was launched with --output_raw
    piper_call = mock_popen.call_args_list[0]
    cmd = piper_call[0][0]
    assert "--output_raw" in cmd
    assert "--model" in cmd

    # Verify aplay was launched with raw PCM params
    aplay_call = mock_popen.call_args_list[1]
    aplay_cmd = aplay_call[0][0]
    assert "aplay" in aplay_cmd[0]
    assert "-r" in aplay_cmd
    assert "22050" in aplay_cmd


def test_speak_feeds_text_to_piper_stdin() -> None:
    """speak() writes the UTF-8 encoded text to piper's stdin."""
    piper_proc = _mock_popen()
    aplay_proc = _mock_popen()

    player = _make_player()

    with patch("subprocess.Popen", side_effect=[piper_proc, aplay_proc]):
        player.speak("sovereign chat", blocking=False)

    piper_proc.stdin.write.assert_called_once_with(b"sovereign chat")
    piper_proc.stdin.close.assert_called_once()


def test_speak_blocking_waits_for_processes() -> None:
    """speak(blocking=True) calls wait() on both piper and aplay."""
    piper_proc = _mock_popen()
    aplay_proc = _mock_popen()

    player = _make_player()

    with patch("subprocess.Popen", side_effect=[piper_proc, aplay_proc]):
        result = player.speak("waiting test", blocking=True)

    assert result is None  # blocking mode returns None
    piper_proc.wait.assert_called_once()
    aplay_proc.wait.assert_called_once()


def test_speak_stores_procs_for_stop() -> None:
    """speak() stores process handles so stop() can terminate them."""
    piper_proc = _mock_popen()
    aplay_proc = _mock_popen()

    player = _make_player()

    with patch("subprocess.Popen", side_effect=[piper_proc, aplay_proc]):
        player.speak("store me", blocking=False)

    assert piper_proc in player._current_procs
    assert aplay_proc in player._current_procs


# ──────────────────────────────────────────────────────────────────────────────
# stop
# ──────────────────────────────────────────────────────────────────────────────


def test_stop_terminates_running_processes() -> None:
    """stop() terminates all tracked processes and clears the list."""
    piper_proc = _mock_popen()
    aplay_proc = _mock_popen()

    player = _make_player()
    player._current_procs = [piper_proc, aplay_proc]

    player.stop()

    piper_proc.terminate.assert_called_once()
    aplay_proc.terminate.assert_called_once()
    assert player._current_procs == []


def test_stop_is_safe_when_no_processes() -> None:
    """stop() does not raise when no processes are tracked."""
    player = _make_player()
    player._current_procs = []
    player.stop()  # should not raise


def test_stop_continues_after_terminate_error() -> None:
    """stop() continues terminating remaining processes even if one raises."""
    bad_proc = MagicMock(spec=subprocess.Popen)
    bad_proc.terminate.side_effect = OSError("already dead")
    good_proc = _mock_popen()

    player = _make_player()
    player._current_procs = [bad_proc, good_proc]

    player.stop()  # should not raise

    good_proc.terminate.assert_called_once()
    assert player._current_procs == []


# ──────────────────────────────────────────────────────────────────────────────
# speak — FileNotFoundError graceful degradation
# ──────────────────────────────────────────────────────────────────────────────


def test_speak_returns_none_on_file_not_found() -> None:
    """speak() returns None and logs a warning when aplay/piper binary is missing."""
    player = _make_player()

    with patch("subprocess.Popen", side_effect=FileNotFoundError("aplay not found")):
        result = player.speak("oops")

    assert result is None


# ──────────────────────────────────────────────────────────────────────────────
# Voice constants
# ──────────────────────────────────────────────────────────────────────────────


def test_default_voice_constant() -> None:
    """DEFAULT_VOICE is the lessac medium voice."""
    assert DEFAULT_VOICE == "en_US-lessac-medium"


def test_lumina_voice_constant() -> None:
    """LUMINA_VOICE is the jenny medium voice."""
    assert LUMINA_VOICE == "en_US-jenny-medium"


# ──────────────────────────────────────────────────────────────────────────────
# Binary discovery
# ──────────────────────────────────────────────────────────────────────────────


def test_find_piper_uses_which_first() -> None:
    """_find_piper() prefers the PATH-discovered binary over hardcoded paths."""
    with patch("shutil.which", return_value="/custom/bin/piper"):
        player = VoicePlayer.__new__(VoicePlayer)
        player._voice = DEFAULT_VOICE
        player._model_path = None
        player._current_procs = []
        result = player._find_piper()

    assert result == "/custom/bin/piper"


def test_find_piper_falls_back_to_search_paths(tmp_path: Path) -> None:
    """_find_piper() falls back to hardcoded paths when not on PATH."""
    fake_piper = tmp_path / "piper"
    fake_piper.touch()

    import skchat.voice as voice_mod

    original = voice_mod._PIPER_SEARCH_PATHS
    voice_mod._PIPER_SEARCH_PATHS = [str(fake_piper)]

    try:
        with patch("shutil.which", return_value=None):
            player = VoicePlayer.__new__(VoicePlayer)
            player._voice = DEFAULT_VOICE
            player._model_path = None
            player._current_procs = []
            result = player._find_piper()
    finally:
        voice_mod._PIPER_SEARCH_PATHS = original

    assert result == str(fake_piper)
