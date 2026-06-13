"""WebSocket transport — FastAPI /ws/voice/{agent} over VoiceEngine.

Ported from skvoice/skvoice/service.py (lines 129-407, 2026-06-12).

Control protocol (backward-compatible with existing webui clients):
    Binary frames     — raw 16-bit PCM (16 kHz mono); accumulated until END_OF_SPEECH
    "END_OF_SPEECH"   — flush PCM buffer → STT → VoiceEngine.respond → TTS → send audio
    "CLEAR_HISTORY"   — clear per-connection history; reply {"type":"status","state":"history_cleared"}
    {"type":"group_context",…}  — buffer a peer-agent message for the next user turn
    {"type":"group_init","peers":[…]}  — append group-mode suffix to the system prompt
    {"type":"inject_session","messages":[…],"emotion_state":"…"}  — restore history from cache
    {"type":"text_message","text":"…"}  — skip STT; go straight to VoiceEngine.respond + TTS

The engine is injected via an `engine_factory(agent_name: str) -> VoiceEngine`
callable so tests can supply a FakeEngine without touching the network.
"""

from __future__ import annotations

import json
import logging
from typing import Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

log = logging.getLogger("skchat.transports.websocket")

# Per-connection histories and buffered group context (module-level so they
# survive reconnects to the same process, mirroring skvoice behaviour).
_histories: dict[str, list[dict]] = {}
_pending_group_context: dict[str, list[dict]] = {}


# ---------------------------------------------------------------------------
# Group-chat helpers (ported verbatim from skvoice/service.py)
# ---------------------------------------------------------------------------


def _build_group_suffix(agent_name: str, peers: list[str]) -> str:
    others = ", ".join(p for p in peers if p and p != agent_name)
    if not others:
        return ""
    return (
        "\n\n[GROUP CHAT — MULTI-AGENT]\n"
        f"You are in a shared chat with these other agents: {others}.\n"
        "They will hear and answer the same user message you do, in parallel.\n"
        "Anything they say will be relayed to you between turns, prefixed like\n"
        "  [from <agent>]: ...\n"
        "Acknowledge them naturally when relevant, but speak as yourself — do "
        "not impersonate them, do not narrate their lines."
    )


def _drain_group_context(conn_id: str) -> str:
    buffered = _pending_group_context.pop(conn_id, None)
    if not buffered:
        return ""
    lines = []
    for entry in buffered:
        sender = entry.get("from", "peer")
        text = (entry.get("text") or "").strip()
        if text:
            lines.append(f"[from {sender}]: {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# App factory (engine injected so tests can swap it)
# ---------------------------------------------------------------------------


def build_app(
    engine_factory: Callable[[str], object] | None = None,
) -> FastAPI:
    """Build the FastAPI app with the WebSocket endpoint wired.

    Args:
        engine_factory: `(agent_name: str) -> VoiceEngine`-like object.
            Defaults to constructing a real VoiceEngine from environment.
    """
    if engine_factory is None:
        engine_factory = _default_engine_factory()

    app = FastAPI(title="skchat-voice", version="2.0.0")

    # Per-app state (scoped to this app instance for test isolation)
    histories: dict[str, list[dict]] = {}
    pending_group: dict[str, list[dict]] = {}

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "skchat-voice"}

    @app.websocket("/ws/voice/{agent_name}")
    async def voice_ws(ws: WebSocket, agent_name: str = "lumina"):
        await ws.accept()
        conn_id = f"{agent_name}:{id(ws)}"
        log.info("WS connected: %s", conn_id)

        engine = engine_factory(agent_name)

        if conn_id not in histories:
            histories[conn_id] = []
        history = histories[conn_id]

        # Mutable system-prompt suffix (extended by group_init)
        group_suffix: list[str] = []
        pcm_buffer = bytearray()

        try:
            while True:
                message = await ws.receive()

                if message.get("type") == "websocket.disconnect":
                    break

                # Binary frame — accumulate PCM
                if message.get("bytes"):
                    pcm_buffer.extend(message["bytes"])
                    continue

                text = message.get("text", "")

                # ── CLEAR_HISTORY ──────────────────────────────────────────
                if text == "CLEAR_HISTORY":
                    history.clear()
                    group_suffix.clear()
                    await ws.send_json({"type": "status", "state": "history_cleared"})
                    log.info("History cleared: %s", conn_id)
                    continue

                # ── JSON control frames ────────────────────────────────────
                try:
                    parsed = json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    parsed = {}

                # group_context — buffer peer-agent messages
                if parsed.get("type") == "group_context":
                    from_agent = parsed.get("from", "unknown")
                    content = (parsed.get("text") or "").strip()
                    if content:
                        pending_group.setdefault(conn_id, []).append(
                            {"from": from_agent, "text": content}
                        )
                    continue

                # group_init — tell the agent it's in a shared room
                if parsed.get("type") == "group_init":
                    peers_raw = parsed.get("peers", []) or []
                    peers = [str(p).strip() for p in peers_raw if str(p).strip()]
                    suffix = _build_group_suffix(agent_name, peers)
                    if suffix:
                        group_suffix.append(suffix)
                    log.info("Group init for %s — peers: %s", conn_id, ", ".join(peers))
                    await ws.send_json({"type": "group_ready", "peers": peers})
                    continue

                # inject_session — restore conversation from browser cache
                if parsed.get("type") == "inject_session":
                    injected = parsed.get("messages", [])
                    history.clear()
                    for msg in injected:
                        if msg.get("role") in ("user", "assistant") and msg.get("content"):
                            history.append({"role": msg["role"], "content": msg["content"]})
                    if len(history) > 40:
                        history[:] = history[-30:]
                    log.info("Session injected: %d messages", len(history))
                    await ws.send_json(
                        {"type": "session_restored", "message_count": len(history)}
                    )
                    continue

                # ── END_OF_SPEECH (voice path) ─────────────────────────────
                if text == "END_OF_SPEECH":
                    if not pcm_buffer:
                        await ws.send_json({"type": "status", "state": "listening"})
                        continue
                    pcm_data = bytes(pcm_buffer)
                    pcm_buffer.clear()
                    try:
                        await _process_speech(
                            ws, pcm_data, history, engine, agent_name,
                            group_suffix=group_suffix, conn_id=conn_id,
                            pending_group=pending_group,
                        )
                    except Exception as exc:
                        log.error("Speech processing error: %s", exc, exc_info=True)
                        await ws.send_json(
                            {"type": "error", "message": f"Processing failed: {exc}"}
                        )
                        await ws.send_json({"type": "status", "state": "listening"})
                    continue

                # ── text_message (text chat path) ──────────────────────────
                if parsed.get("type") == "text_message" and parsed.get("text"):
                    try:
                        await _process_text(
                            ws, parsed["text"], history, engine, agent_name,
                            group_suffix=group_suffix, conn_id=conn_id,
                            pending_group=pending_group,
                        )
                    except Exception as exc:
                        log.error("Text processing error: %s", exc, exc_info=True)
                        await ws.send_json(
                            {"type": "error", "message": f"Processing failed: {exc}"}
                        )
                        await ws.send_json({"type": "status", "state": "listening"})
                    continue

        except WebSocketDisconnect:
            log.info("WS disconnected: %s", conn_id)
        except Exception as exc:
            log.error("WS error for %s: %s", conn_id, exc, exc_info=True)
        finally:
            histories.pop(conn_id, None)
            pending_group.pop(conn_id, None)
            log.info("WS closed: %s", conn_id)

    return app


# ---------------------------------------------------------------------------
# Speech and text processing helpers
# ---------------------------------------------------------------------------


async def _process_speech(
    ws: WebSocket,
    pcm_data: bytes,
    history: list[dict],
    engine,
    agent_name: str,
    *,
    group_suffix: list[str],
    conn_id: str,
    pending_group: dict,
) -> None:
    """Full voice pipeline: PCM → STT → VoiceEngine → TTS → send audio."""
    await ws.send_json({"type": "status", "state": "processing"})

    # 1. STT — use the engine's STT client if available, else fall back to
    #    a direct call so the transport works with a bare engine stub.
    try:
        from skchat.voice_engine.audio_codec import pcm_to_wav  # noqa: PLC0415
        wav_data = pcm_to_wav(pcm_data)
    except Exception:
        wav_data = pcm_data  # best effort

    transcript = ""
    stt_client = getattr(engine, "stt", None)
    if stt_client is not None:
        try:
            transcript = await stt_client.transcribe(wav_data)
        except Exception as exc:
            log.warning("STT failed: %s", exc)
    else:
        # Direct skchat STT path (skvoice compat)
        try:
            from skchat.voice import transcribe  # noqa: PLC0415
            transcript = await transcribe(wav_data)
        except Exception:
            pass

    if not transcript:
        await ws.send_json({"type": "status", "state": "listening"})
        return

    await ws.send_json({"type": "transcript", "role": "user", "text": transcript})
    await ws.send_json({"type": "status", "state": "thinking"})

    # Prepend buffered peer messages
    peer_block = _drain_peer_context(conn_id, pending_group)
    llm_input = f"{peer_block}\n\n{transcript}" if peer_block else transcript

    # Build mode/speaker from context (defaults for now — Phase 3 adds proper auth)
    mode = "group" if group_suffix else "sacred"
    response = await engine.respond(
        llm_input, history, mode=mode, speaker_id="chef", is_operator=True
    )

    history.append({"role": "user", "content": llm_input})
    history.append({"role": "assistant", "content": response})
    if len(history) > 40:
        history[:] = history[-30:]

    await ws.send_json({"type": "transcript", "role": "assistant", "text": response})
    await ws.send_json({"type": "status", "state": "speaking"})

    # TTS
    audio_bytes = await _synthesize(engine, response, agent_name)
    if audio_bytes:
        await ws.send_bytes(audio_bytes)

    await ws.send_json({"type": "status", "state": "listening"})


async def _process_text(
    ws: WebSocket,
    text: str,
    history: list[dict],
    engine,
    agent_name: str,
    *,
    group_suffix: list[str],
    conn_id: str,
    pending_group: dict,
) -> None:
    """Text chat path: skip STT → VoiceEngine.respond → optional TTS → send."""
    await ws.send_json({"type": "status", "state": "thinking"})

    peer_block = _drain_peer_context(conn_id, pending_group)
    llm_input = f"{peer_block}\n\n{text}" if peer_block else text

    mode = "group" if group_suffix else "sacred"
    response = await engine.respond(
        llm_input, history, mode=mode, speaker_id="chef", is_operator=True
    )

    history.append({"role": "user", "content": llm_input})
    history.append({"role": "assistant", "content": response})
    if len(history) > 40:
        history[:] = history[-30:]

    await ws.send_json({"type": "transcript", "role": "assistant", "text": response})
    await ws.send_json({"type": "status", "state": "speaking"})

    audio_bytes = await _synthesize(engine, response, agent_name)
    if audio_bytes:
        await ws.send_bytes(audio_bytes)

    await ws.send_json({"type": "status", "state": "listening"})


# ---------------------------------------------------------------------------
# TTS helper — delegates to the engine's TTS client or falls back
# ---------------------------------------------------------------------------


async def _synthesize(engine, text: str, agent_name: str) -> bytes:
    tts_client = getattr(engine, "tts", None)
    if tts_client is not None:
        try:
            return await tts_client.synthesize(text)
        except Exception as exc:
            log.warning("TTS (engine) failed: %s", exc)
            return b""
    # Fallback to skvoice synthesize (skvoice compat shim)
    try:
        from skchat.voice import synthesize  # noqa: PLC0415
        cfg = getattr(engine, "cfg", None)
        voice = getattr(cfg, "tts_voice", agent_name) if cfg else agent_name
        return await synthesize(text, voice=voice)
    except Exception as exc:
        log.warning("TTS fallback failed: %s", exc)
        return b""


# ---------------------------------------------------------------------------
# Peer-context drain (scoped to per-app dict)
# ---------------------------------------------------------------------------


def _drain_peer_context(conn_id: str, pending_group: dict) -> str:
    buffered = pending_group.pop(conn_id, None)
    if not buffered:
        return ""
    lines = []
    for entry in buffered:
        sender = entry.get("from", "peer")
        text = (entry.get("text") or "").strip()
        if text:
            lines.append(f"[from {sender}]: {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Default engine factory
# ---------------------------------------------------------------------------


def _default_engine_factory() -> Callable[[str], object]:
    """Build real VoiceEngines from the environment."""

    def factory(agent_name: str):
        from skchat.voice_engine.builtin_tools import build_default_registry  # noqa: PLC0415
        from skchat.voice_engine.config import VoiceConfig  # noqa: PLC0415
        from skchat.voice_engine.engine import VoiceEngine  # noqa: PLC0415

        cfg = VoiceConfig.from_env()
        registry = build_default_registry(cfg, agent_name)
        return VoiceEngine(cfg, agent_name, registry=registry)

    return factory
