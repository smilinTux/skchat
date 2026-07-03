"""skchat.transports — pluggable transport layer over the VoiceEngine.

Current transports:
    websocket.py  — FastAPI /ws/voice/{agent} (text + binary PCM)
    serve_ws.py   — uvicorn entrypoint for the WebSocket transport
    livekit.py    — LiveKit room agent (energy VAD + barge-in + addressing/
                    roundtable gate) over the VoiceEngine brain. `livekit` is a
                    soft dependency — importing the module never requires it.
"""
