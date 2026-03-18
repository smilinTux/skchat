"""FaceTime session manager for skchat.

skchat acts as a thin proxy for FaceTime signaling. The actual WebRTC
peer connection lives on the GPU server (192.168.0.100) alongside
MuseTalk and TTS. skchat provides:

1. The FaceTime HTML page (/facetime, /facetime/{agent})
2. WebSocket signaling proxy (shares the existing /webrtc/ws broker)
3. WebSocket fallback endpoint (/ws/facetime/{agent}) for when WebRTC fails
4. Session management API (/api/facetime/sessions)

Architecture:
    Browser ─── WebRTC (direct ICE) ──── GPU Server (aiortc + MuseTalk)
       │                                      │
       └── signaling ── skchat /webrtc/ws ────┘
              (SDP/ICE relay only, no media)

    Fallback (no WebRTC):
    Browser ─── WS /ws/facetime/{agent} ── skchat (proxy) ── GPU SKVoice

Dependencies:
    - SKComm signaling broker (already running as part of skchat/skcomm serve)
    - SKVoice on GPU box (192.168.0.100:18800) for voice pipeline
    - FaceTimeSession on GPU box (new: manages WebRTC media tracks)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
from pathlib import Path

import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse

logger = logging.getLogger("skchat.facetime")

# SKVoice FaceTime endpoint on GPU box
SKVOICE_FACETIME_URL = os.getenv(
    "SKCHAT_SKVOICE_FACETIME_URL", "ws://192.168.0.100:18800/ws/facetime"
)
DEFAULT_AGENT = os.getenv("SKCHAT_FACETIME_AGENT", "lumina")


def register_facetime_routes(app: FastAPI) -> None:
    """Register FaceTime page and WebSocket fallback routes.

    The signaling WebSocket (/webrtc/ws) is already registered by SKComm.
    This registers:
        - GET /facetime — FaceTime page (default agent)
        - GET /facetime/{agent} — FaceTime page for specific agent
        - WS /ws/facetime/{agent} — WebSocket fallback (MJPEG + Opus)
        - GET /api/facetime/sessions — Active session info

    Args:
        app: FastAPI application instance.
    """

    @app.get("/facetime", response_class=HTMLResponse)
    async def facetime_page():
        """Serve the FaceTime HTML page."""
        static = Path(__file__).parent / "static" / "facetime.html"
        if static.exists():
            return FileResponse(static, media_type="text/html")
        return HTMLResponse("<h1>facetime.html not found</h1>", status_code=404)

    @app.get("/facetime/{agent_name}", response_class=HTMLResponse)
    async def facetime_agent_page(agent_name: str):
        """Serve the FaceTime HTML page for a specific agent."""
        static = Path(__file__).parent / "static" / "facetime.html"
        if static.exists():
            return FileResponse(static, media_type="text/html")
        return HTMLResponse("<h1>facetime.html not found</h1>", status_code=404)

    @app.get("/api/facetime/agents")
    async def facetime_agents():
        """Return list of agents that support FaceTime.

        Checks which agents have portraits available.
        """
        agents_dir = Path.home() / ".skcapstone" / "agents"
        available = []
        if agents_dir.exists():
            for agent_dir in agents_dir.iterdir():
                if agent_dir.is_dir():
                    portrait = agent_dir / "avatar" / "portrait.png"
                    available.append({
                        "name": agent_dir.name,
                        "has_portrait": portrait.exists(),
                        "portrait_url": f"/api/facetime/portrait/{agent_dir.name}"
                            if portrait.exists() else None,
                    })
        return {"agents": available}

    @app.get("/api/facetime/portrait/{agent_name}")
    async def facetime_portrait(agent_name: str):
        """Serve an agent's portrait image."""
        portrait = (
            Path.home() / ".skcapstone" / "agents" / agent_name
            / "avatar" / "portrait.png"
        )
        if portrait.exists():
            return FileResponse(portrait, media_type="image/png")
        return HTMLResponse("Portrait not found", status_code=404)

    @app.websocket("/ws/facetime/{agent_name}")
    async def facetime_ws_fallback(ws: WebSocket, agent_name: str):
        """WebSocket fallback for FaceTime when WebRTC is unavailable.

        Proxies to SKVoice on the GPU box. SKVoice sends:
        - Binary frames: [4B type][4B ts][4B len][payload]
            type 0x01 = JPEG video frame
            type 0x02 = Opus audio packet
        - Text frames: JSON control messages (transcript, emotion, status)
        """
        await _proxy_facetime_ws(ws, agent_name)

    logger.info(
        "FaceTime routes registered: /facetime, /ws/facetime/{agent}, "
        "/api/facetime/*"
    )


async def _proxy_facetime_ws(client_ws: WebSocket, agent_name: str) -> None:
    """Bidirectional WebSocket proxy to SKVoice FaceTime on GPU box.

    Same pattern as voice_ws_lite._proxy_voice but for the FaceTime
    endpoint which includes video frames.
    """
    await client_ws.accept()

    backend_url = f"{SKVOICE_FACETIME_URL}/{agent_name}"
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
            await client_ws.send_json({
                "type": "error",
                "message": f"FaceTime service unavailable: {e}",
            })
        except Exception:
            pass
    finally:
        try:
            await client_ws.close()
        except Exception:
            pass
