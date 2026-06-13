"""skchat.transports — pluggable transport layer over the VoiceEngine.

Current transports:
    websocket.py  — FastAPI /ws/voice/{agent} (text + binary PCM)
    serve_ws.py   — uvicorn entrypoint for the WebSocket transport
"""
