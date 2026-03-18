"""Real-time voice streaming for skchat.

Pipeline: Browser audio -> WebSocket -> VAD -> STT -> LLM -> TTS -> WebSocket -> Browser

Uses:
- Silero VAD for speech boundary detection (CPU, torch)
- faster-whisper API on .100 for STT (OpenAI-compatible)
- Ollama LLM for response generation (OpenAI-compatible chat completions)
- Chatterbox TTS on .100 for speech synthesis (OpenAI-compatible)
- Piper TTS as local CPU fallback

Audio format: 16-bit PCM, 16kHz mono (standard for speech processing)
WebSocket protocol: binary frames for audio, text/JSON frames for metadata
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import struct
import wave
from pathlib import Path
from typing import Optional

import httpx
import torch

logger = logging.getLogger("skchat.voice_stream")

# ---------------------------------------------------------------------------
# Configuration (env vars with sensible defaults)
# ---------------------------------------------------------------------------

WHISPER_URL = os.getenv("SKCHAT_STT_URL", "http://192.168.0.100:18794/v1/audio/transcriptions")
CHATTERBOX_URL = os.getenv("SKCHAT_TTS_URL", "http://192.168.0.100:18793/audio/speech")
OLLAMA_URL = os.getenv("SKCHAT_LLM_URL", "http://192.168.0.100:11434/v1/chat/completions")
OLLAMA_MODEL = os.getenv("SKCHAT_LLM_MODEL", "kimi-k2-instruct")
PIPER_BIN = os.getenv("SKCHAT_PIPER_BIN", "/usr/local/piper/piper")
PIPER_MODEL = os.getenv(
    "SKCHAT_PIPER_MODEL",
    str(Path.home() / ".local/share/piper/voices/en_US-lessac-medium.onnx"),
)

# Audio constants
SAMPLE_RATE = 16000  # 16 kHz
SAMPLE_WIDTH = 2  # 16-bit = 2 bytes
CHANNELS = 1  # mono

# VAD parameters
VAD_THRESHOLD = 0.5
MIN_SPEECH_MS = 250
MIN_SILENCE_MS = 600  # ms of silence to consider speech ended
VAD_WINDOW_SIZE = 512  # Silero expects 512 samples at 16kHz

# System prompt for the LLM
SYSTEM_PROMPT = os.getenv(
    "SKCHAT_VOICE_SYSTEM_PROMPT",
    "You are Lumina, a helpful AI assistant. Keep responses concise and conversational "
    "since they will be spoken aloud. Aim for 1-3 sentences unless the user asks for detail.",
)


# ---------------------------------------------------------------------------
# Silero VAD wrapper
# ---------------------------------------------------------------------------


class SileroVAD:
    """Voice Activity Detection using Silero VAD (torch, CPU-only)."""

    def __init__(self, threshold: float = VAD_THRESHOLD):
        self.threshold = threshold
        self._model = None
        self._lock = asyncio.Lock()

    def _load_model(self):
        if self._model is None:
            from silero_vad import load_silero_vad

            self._model = load_silero_vad()
            logger.info("Silero VAD model loaded")

    def reset(self):
        """Reset VAD state between utterances."""
        if self._model is not None:
            self._model.reset_states()

    def is_speech(self, audio_chunk: bytes) -> bool:
        """Check if a 512-sample audio chunk contains speech.

        Args:
            audio_chunk: Raw 16-bit PCM audio, 16kHz mono, 512 samples (1024 bytes).

        Returns:
            True if speech probability exceeds threshold.
        """
        self._load_model()

        # Convert bytes to float32 tensor
        n_samples = len(audio_chunk) // SAMPLE_WIDTH
        samples = struct.unpack(f"<{n_samples}h", audio_chunk[: n_samples * SAMPLE_WIDTH])
        tensor = torch.FloatTensor(samples) / 32768.0

        # Silero expects specific window sizes at 16kHz: 512, 1024, or 1536
        if len(tensor) < VAD_WINDOW_SIZE:
            # Pad with zeros if too short
            tensor = torch.nn.functional.pad(tensor, (0, VAD_WINDOW_SIZE - len(tensor)))

        with torch.no_grad():
            prob = self._model(tensor, SAMPLE_RATE).item()

        return prob > self.threshold


# ---------------------------------------------------------------------------
# STT: faster-whisper API (OpenAI-compatible)
# ---------------------------------------------------------------------------


async def transcribe_audio(pcm_data: bytes, client: httpx.AsyncClient) -> Optional[str]:
    """Send PCM audio to the faster-whisper API for transcription.

    Args:
        pcm_data: Raw 16-bit PCM audio, 16kHz mono.
        client: Shared httpx client.

    Returns:
        Transcribed text or None on failure.
    """
    # Wrap PCM in a WAV container for the API
    wav_buf = io.BytesIO()
    with wave.open(wav_buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_data)
    wav_bytes = wav_buf.getvalue()

    try:
        resp = await client.post(
            WHISPER_URL,
            files={"file": ("audio.wav", wav_bytes, "audio/wav")},
            data={"model": "whisper-1", "language": "en"},
            timeout=15.0,
        )
        resp.raise_for_status()
        result = resp.json()
        text = result.get("text", "").strip()
        return text if text else None
    except httpx.ConnectError:
        logger.warning("STT service unreachable at %s", WHISPER_URL)
        return None
    except Exception as exc:
        logger.warning("STT transcription failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# LLM: Ollama OpenAI-compatible chat completions
# ---------------------------------------------------------------------------


async def generate_response(
    transcript: str,
    conversation: list[dict],
    client: httpx.AsyncClient,
) -> str:
    """Send transcript to Ollama LLM and return the response text.

    Args:
        transcript: User's transcribed speech.
        conversation: Message history for context.
        client: Shared httpx client.

    Returns:
        LLM response text, or a fallback error message.
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    # Include recent conversation for context (last 10 turns)
    messages.extend(conversation[-10:])
    messages.append({"role": "user", "content": transcript})

    try:
        resp = await client.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "messages": messages,
                "stream": False,
                "max_tokens": 256,
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()
        return content
    except httpx.ConnectError:
        logger.warning("LLM service unreachable at %s", OLLAMA_URL)
        return "I'm sorry, the language model is currently unavailable."
    except Exception as exc:
        logger.warning("LLM generation failed: %s", exc)
        return "I'm sorry, I encountered an error generating a response."


# ---------------------------------------------------------------------------
# TTS: Chatterbox API (OpenAI-compatible) with Piper fallback
# ---------------------------------------------------------------------------


async def synthesize_speech(text: str, client: httpx.AsyncClient) -> Optional[bytes]:
    """Convert text to speech audio (PCM 16-bit, 16kHz mono).

    Tries Chatterbox TTS first, falls back to local Piper TTS.

    Args:
        text: Text to synthesize.
        client: Shared httpx client.

    Returns:
        Raw PCM audio bytes (16-bit, 16kHz, mono) or None on total failure.
    """
    # Try Chatterbox first
    pcm = await _tts_chatterbox(text, client)
    if pcm is not None:
        return pcm

    # Fall back to local Piper
    logger.info("Chatterbox unavailable, falling back to Piper TTS")
    return await _tts_piper(text)


async def _tts_chatterbox(text: str, client: httpx.AsyncClient) -> Optional[bytes]:
    """Synthesize via Chatterbox TTS API (returns WAV, we extract PCM)."""
    try:
        resp = await client.post(
            CHATTERBOX_URL,
            json={
                "model": "tts-1",
                "input": text,
                "voice": "lumina",
                "response_format": "wav",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        wav_data = resp.content

        # Parse WAV to get raw PCM, then resample to 16kHz if needed
        return _wav_to_pcm_16k(wav_data)
    except httpx.ConnectError:
        logger.warning("Chatterbox TTS unreachable at %s", CHATTERBOX_URL)
        return None
    except Exception as exc:
        logger.warning("Chatterbox TTS failed: %s", exc)
        return None


def _wav_to_pcm_16k(wav_data: bytes) -> Optional[bytes]:
    """Extract PCM from WAV and resample to 16kHz mono 16-bit if needed."""
    try:
        buf = io.BytesIO(wav_data)
        with wave.open(buf, "rb") as wf:
            orig_rate = wf.getframerate()
            orig_channels = wf.getnchannels()
            orig_width = wf.getsampwidth()
            frames = wf.readframes(wf.getnframes())

        # Convert to mono if stereo
        if orig_channels == 2:
            samples = struct.unpack(f"<{len(frames) // 2}h", frames)
            mono = []
            for i in range(0, len(samples), 2):
                mono.append((samples[i] + samples[i + 1]) // 2)
            frames = struct.pack(f"<{len(mono)}h", *mono)

        # Handle sample width conversion (e.g., 24-bit or 32-bit to 16-bit)
        if orig_width != 2:
            # Simple case: just reinterpret. For real production, use proper conversion.
            # This handles the common 16-bit case; other widths are rare for TTS.
            logger.warning("TTS returned %d-bit audio, expected 16-bit", orig_width * 8)

        # Resample if not 16kHz
        if orig_rate != SAMPLE_RATE:
            samples = struct.unpack(f"<{len(frames) // 2}h", frames)
            tensor = torch.FloatTensor(samples) / 32768.0
            # Simple linear resampling via torch
            tensor = tensor.unsqueeze(0)
            target_len = int(len(samples) * SAMPLE_RATE / orig_rate)
            resampled = torch.nn.functional.interpolate(
                tensor.unsqueeze(0), size=target_len, mode="linear", align_corners=False
            ).squeeze()
            int_samples = (resampled * 32767).clamp(-32768, 32767).to(torch.int16)
            frames = int_samples.numpy().tobytes()

        return frames
    except Exception as exc:
        logger.warning("WAV to PCM conversion failed: %s", exc)
        return None


async def _tts_piper(text: str) -> Optional[bytes]:
    """Synthesize via local Piper TTS binary (CPU, instant)."""
    piper_bin = PIPER_BIN
    piper_model = PIPER_MODEL

    if not Path(piper_bin).exists():
        logger.warning("Piper binary not found at %s", piper_bin)
        return None
    if not Path(piper_model).exists():
        logger.warning("Piper model not found at %s", piper_model)
        return None

    try:
        # Piper outputs raw PCM at 22050 Hz by default
        proc = await asyncio.create_subprocess_exec(
            piper_bin,
            "--model",
            piper_model,
            "--output_raw",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(text.encode("utf-8")), timeout=15.0)

        if proc.returncode != 0 or not stdout:
            return None

        # Piper outputs 22050 Hz — resample to 16000 Hz
        samples = struct.unpack(f"<{len(stdout) // 2}h", stdout)
        tensor = torch.FloatTensor(samples) / 32768.0
        target_len = int(len(samples) * SAMPLE_RATE / 22050)
        resampled = torch.nn.functional.interpolate(
            tensor.unsqueeze(0).unsqueeze(0),
            size=target_len,
            mode="linear",
            align_corners=False,
        ).squeeze()
        int_samples = (resampled * 32767).clamp(-32768, 32767).to(torch.int16)
        return int_samples.numpy().tobytes()
    except asyncio.TimeoutError:
        logger.warning("Piper TTS timed out")
        return None
    except Exception as exc:
        logger.warning("Piper TTS failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Voice Stream Manager — orchestrates the full pipeline per WebSocket session
# ---------------------------------------------------------------------------


class VoiceStreamManager:
    """Manages a single voice streaming session over WebSocket.

    Lifecycle:
        1. Browser connects via WebSocket to /ws/voice
        2. Browser sends binary PCM audio chunks (16-bit, 16kHz, mono)
        3. VAD detects speech boundaries
        4. On speech end: STT -> LLM -> TTS -> send audio back
        5. JSON text messages carry metadata (transcript, status, etc.)
    """

    def __init__(self):
        self.vad = SileroVAD()
        self._audio_buffer = bytearray()
        self._speech_audio = bytearray()
        self._is_speaking = False
        self._silence_frames = 0
        self._speech_frames = 0
        self._conversation: list[dict] = []  # chat history for LLM context
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient()
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def handle_websocket(self, websocket) -> None:
        """Main WebSocket handler for a voice session.

        Args:
            websocket: FastAPI WebSocket connection (already accepted).
        """
        from fastapi import WebSocketDisconnect

        logger.info("Voice stream session started")
        await self._send_status(websocket, "connected", "Voice chat ready. Start speaking.")

        try:
            while True:
                message = await websocket.receive()

                if message.get("type") == "websocket.disconnect":
                    break

                if "bytes" in message and message["bytes"]:
                    # Binary frame: audio data
                    await self._process_audio(websocket, message["bytes"])
                elif "text" in message and message["text"]:
                    # Text frame: control messages
                    await self._handle_control(websocket, message["text"])

        except WebSocketDisconnect:
            logger.info("Voice stream client disconnected")
        except Exception as exc:
            logger.error("Voice stream error: %s", exc)
        finally:
            await self.close()
            logger.info("Voice stream session ended")

    async def _on_speech_end(self, websocket) -> None:
        """Called when VAD detects end of speech. Runs STT -> LLM -> TTS."""
        logger.info(
            "Speech ended, processing utterance (%d bytes buffered)", len(self._audio_buffer)
        )

        # Collect all audio accumulated during speech
        pcm_data = bytes(self._speech_audio)

        # Reset speech state
        self._is_speaking = False
        self._silence_frames = 0
        self._speech_frames = 0
        self.vad.reset()

        if len(pcm_data) < SAMPLE_RATE * SAMPLE_WIDTH // 2:
            # Less than 0.5s of audio — probably noise
            await self._send_status(websocket, "ready", "Ready")
            return

        await self._send_status(websocket, "processing", "Transcribing...")

        client = await self._get_client()

        # STT
        transcript = await transcribe_audio(pcm_data, client)
        if not transcript:
            await self._send_status(websocket, "ready", "Could not understand audio.")
            return

        await self._send_transcript(websocket, "user", transcript)
        logger.info("Transcribed: %s", transcript)

        # LLM
        await self._send_status(websocket, "thinking", "Thinking...")
        response_text = await generate_response(transcript, self._conversation, client)

        # Update conversation history
        self._conversation.append({"role": "user", "content": transcript})
        self._conversation.append({"role": "assistant", "content": response_text})

        await self._send_transcript(websocket, "assistant", response_text)
        logger.info("Response: %s", response_text)

        # TTS
        await self._send_status(websocket, "speaking", "Speaking...")
        audio_out = await synthesize_speech(response_text, client)

        if audio_out:
            # Send audio in chunks to avoid overwhelming the WebSocket
            chunk_size = 8192  # 8KB chunks (~256ms at 16kHz 16-bit)
            for i in range(0, len(audio_out), chunk_size):
                await websocket.send_bytes(audio_out[i : i + chunk_size])
            # Send end-of-audio marker
            await websocket.send_text(json.dumps({"type": "audio_end"}))
        else:
            await self._send_status(websocket, "error", "TTS synthesis failed")

        await self._send_status(websocket, "ready", "Ready")

    async def _process_audio(self, websocket, audio_bytes: bytes) -> None:
        """Process incoming audio chunk through VAD pipeline.

        Accumulates speech audio and triggers STT->LLM->TTS on speech end.
        """
        self._audio_buffer.extend(audio_bytes)

        chunk_bytes = VAD_WINDOW_SIZE * SAMPLE_WIDTH

        while len(self._audio_buffer) >= chunk_bytes:
            chunk = bytes(self._audio_buffer[:chunk_bytes])
            del self._audio_buffer[:chunk_bytes]

            speech_detected = self.vad.is_speech(chunk)

            if speech_detected:
                self._speech_frames += 1
                self._silence_frames = 0
                self._speech_audio.extend(chunk)

                if not self._is_speaking:
                    min_frames = int(MIN_SPEECH_MS * SAMPLE_RATE / 1000 / VAD_WINDOW_SIZE)
                    if self._speech_frames >= min_frames:
                        self._is_speaking = True
                        await self._send_status(websocket, "listening", "Listening...")
                        logger.debug("Speech started")
            else:
                if self._is_speaking:
                    self._silence_frames += 1
                    self._speech_audio.extend(chunk)  # include trailing silence
                    silence_ms = self._silence_frames * VAD_WINDOW_SIZE * 1000 / SAMPLE_RATE
                    if silence_ms >= MIN_SILENCE_MS:
                        await self._on_speech_end(websocket)
                        self._speech_audio = bytearray()
                else:
                    self._speech_frames = 0
                    # Keep a small pre-roll buffer (last 3 chunks) for speech onset
                    self._speech_audio = bytearray(chunk)

    async def _handle_control(self, websocket, text: str) -> None:
        """Handle JSON control messages from the browser."""
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type", "")

        if msg_type == "ping":
            await websocket.send_text(json.dumps({"type": "pong"}))
        elif msg_type == "clear_history":
            self._conversation.clear()
            await self._send_status(websocket, "ready", "Conversation cleared.")
        elif msg_type == "config":
            # Allow client to override some settings
            if "system_prompt" in msg:
                global SYSTEM_PROMPT
                SYSTEM_PROMPT = msg["system_prompt"]
            await self._send_status(websocket, "ready", "Config updated.")

    async def _send_status(self, websocket, status: str, message: str) -> None:
        """Send a status update to the browser."""
        await websocket.send_text(
            json.dumps(
                {
                    "type": "status",
                    "status": status,
                    "message": message,
                }
            )
        )

    async def _send_transcript(self, websocket, role: str, text: str) -> None:
        """Send a transcript line to the browser."""
        await websocket.send_text(
            json.dumps(
                {
                    "type": "transcript",
                    "role": role,
                    "text": text,
                }
            )
        )


# ---------------------------------------------------------------------------
# FastAPI route registration
# ---------------------------------------------------------------------------


def register_voice_routes(app) -> None:
    """Register voice streaming WebSocket route and static file serving on a FastAPI app.

    Args:
        app: FastAPI application instance.
    """
    from fastapi import WebSocket as WS
    from fastapi.responses import FileResponse, HTMLResponse

    static_dir = Path(__file__).parent / "static"

    @app.websocket("/ws/voice")
    async def ws_voice(websocket: WS) -> None:
        """WebSocket endpoint for real-time voice streaming."""
        await websocket.accept()
        manager = VoiceStreamManager()
        await manager.handle_websocket(websocket)

    @app.get("/voice", response_class=HTMLResponse)
    async def voice_chat_page() -> FileResponse:
        """Serve the voice chat HTML page."""
        html_path = static_dir / "voice-chat.html"
        if html_path.exists():
            return FileResponse(html_path, media_type="text/html")
        return HTMLResponse("<h1>voice-chat.html not found</h1>", status_code=404)

    logger.info("Voice streaming routes registered: /ws/voice, /voice")
