"""Voice WebSocket proxy — forwards to SKVoice service on GPU box.

skchat is now a thin proxy. All voice processing (STT, emotion detection,
agent profile loading, LLM, TTS) happens on the GPU box via SKVoice.

Browser ↔ skchat (Traefik/TLS) ↔ SKVoice (.100 GPU)
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse

# SKVoice service on GPU box — handles the entire voice pipeline
SKVOICE_URL = os.getenv("SKCHAT_SKVOICE_URL", "ws://192.168.0.100:18800/ws/voice")
# SKVoice video endpoint (MuseTalk lip-synced avatar pipeline)
SKVOICE_VIDEO_URL = os.getenv("SKCHAT_SKVOICE_VIDEO_URL", "ws://192.168.0.100:18800/ws/video")
DEFAULT_AGENT = os.getenv("SKCHAT_VOICE_AGENT", "lumina")


def register_voice_routes_lite(app: FastAPI) -> None:
    """Register voice WebSocket proxy and page routes."""

    @app.get("/voice", response_class=HTMLResponse)
    async def voice_chat_page():
        static = Path(__file__).parent / "static" / "voice-chat.html"
        if static.exists():
            return FileResponse(static, media_type="text/html")
        return HTMLResponse("<h1>voice-chat.html not found</h1>", status_code=404)

    @app.get("/voice/{agent_name}", response_class=HTMLResponse)
    async def voice_chat_agent_page(agent_name: str):
        """Voice chat page for a specific agent."""
        static = Path(__file__).parent / "static" / "voice-chat.html"
        if static.exists():
            return FileResponse(static, media_type="text/html")
        return HTMLResponse("<h1>voice-chat.html not found</h1>", status_code=404)

    @app.get("/voice-group", response_class=HTMLResponse)
    async def voice_group_page():
        """Group voice chat page — talk to multiple agents at once."""
        static = Path(__file__).parent / "static" / "voice-group.html"
        if static.exists():
            return FileResponse(static, media_type="text/html")
        return HTMLResponse("<h1>voice-group.html not found</h1>", status_code=404)

    @app.get("/voice-agents-api")
    async def voice_agents_api():
        """Return list of available agents from SKVoice."""
        import httpx

        skvoice_http = SKVOICE_URL.replace("ws://", "http://").replace("/ws/voice", "")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{skvoice_http}/voice/agents")
                return resp.json()
        except Exception:
            return {"agents": ["lumina", "jarvis", "opus"]}

    @app.websocket("/ws/voice")
    async def voice_websocket(ws: WebSocket):
        await _proxy_voice(ws, DEFAULT_AGENT)

    @app.websocket("/ws/voice/{agent_name}")
    async def voice_websocket_agent(ws: WebSocket, agent_name: str):
        await _proxy_voice(ws, agent_name)

    # ── Video Chat routes ──

    @app.get("/video", response_class=HTMLResponse)
    async def video_chat_page():
        static = Path(__file__).parent / "static" / "video-chat.html"
        if static.exists():
            return FileResponse(static, media_type="text/html")
        return HTMLResponse("<h1>video-chat.html not found</h1>", status_code=404)

    @app.get("/video/{agent_name}", response_class=HTMLResponse)
    async def video_chat_agent_page(agent_name: str):
        """Video chat page for a specific agent (MuseTalk lip-synced avatar)."""
        static = Path(__file__).parent / "static" / "video-chat.html"
        if static.exists():
            return FileResponse(static, media_type="text/html")
        return HTMLResponse("<h1>video-chat.html not found</h1>", status_code=404)

    @app.websocket("/ws/video")
    async def video_websocket(ws: WebSocket):
        await _proxy_video(ws, DEFAULT_AGENT)

    @app.websocket("/ws/video/{agent_name}")
    async def video_websocket_agent(ws: WebSocket, agent_name: str):
        await _proxy_video(ws, agent_name)


async def _proxy_voice(client_ws: WebSocket, agent_name: str) -> None:
    """Bidirectional WebSocket proxy to SKVoice on GPU box."""
    await client_ws.accept()

    backend_url = f"{SKVOICE_URL}/{agent_name}"
    try:
        async with websockets.connect(backend_url) as backend_ws:

            async def client_to_backend():
                """Forward browser messages to SKVoice."""
                try:
                    while True:
                        data = await client_ws.receive()
                        if data.get("type") == "websocket.disconnect":
                            break
                        if "bytes" in data and data["bytes"]:
                            await backend_ws.send(data["bytes"])
                        elif "text" in data and data["text"]:
                            await backend_ws.send(data["text"])
                except (WebSocketDisconnect, RuntimeError):
                    pass

            async def backend_to_client():
                """Forward SKVoice responses to browser."""
                try:
                    async for msg in backend_ws:
                        if isinstance(msg, bytes):
                            await client_ws.send_bytes(msg)
                        else:
                            await client_ws.send_text(msg)
                except websockets.exceptions.ConnectionClosed:
                    pass

            # Run both directions concurrently
            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(client_to_backend()),
                    asyncio.create_task(backend_to_client()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

    except Exception as e:
        try:
            await client_ws.send_json(
                {
                    "type": "error",
                    "message": f"Voice service unavailable: {e}",
                }
            )
        except Exception:
            pass
    finally:
        try:
            await client_ws.close()
        except Exception:
            pass


async def _proxy_video(client_ws: WebSocket, agent_name: str) -> None:
    """Bidirectional WebSocket proxy to SKVoice video pipeline on GPU box.

    The video pipeline extends the voice pipeline with MuseTalk lip-synced
    avatar frames. The server sends:
      - JSON status/transcript messages (text)
      - Binary audio frames (TTS)
      - JSON {type: "video_frame"} followed by binary JPEG/WebP frame data

    The client sends:
      - Binary PCM audio frames (16kHz mono 16-bit)
      - JSON control messages (mute, avatar_on/off, text_message, etc.)
    """
    await client_ws.accept()

    backend_url = f"{SKVOICE_VIDEO_URL}/{agent_name}"
    try:
        async with websockets.connect(
            backend_url,
            max_size=2**22,  # 4MB — video frames can be large
        ) as backend_ws:

            async def client_to_backend():
                """Forward browser messages to SKVoice video pipeline."""
                try:
                    while True:
                        data = await client_ws.receive()
                        if data.get("type") == "websocket.disconnect":
                            break
                        if "bytes" in data and data["bytes"]:
                            await backend_ws.send(data["bytes"])
                        elif "text" in data and data["text"]:
                            await backend_ws.send(data["text"])
                except (WebSocketDisconnect, RuntimeError):
                    pass

            async def backend_to_client():
                """Forward SKVoice video responses to browser."""
                try:
                    async for msg in backend_ws:
                        if isinstance(msg, bytes):
                            await client_ws.send_bytes(msg)
                        else:
                            await client_ws.send_text(msg)
                except websockets.exceptions.ConnectionClosed:
                    pass

            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(client_to_backend()),
                    asyncio.create_task(backend_to_client()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

    except Exception as e:
        try:
            await client_ws.send_json(
                {
                    "type": "error",
                    "message": f"Video service unavailable: {e}",
                }
            )
        except Exception:
            pass
    finally:
        try:
            await client_ws.close()
        except Exception:
            pass
