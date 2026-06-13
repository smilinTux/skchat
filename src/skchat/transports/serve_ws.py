"""skchat-voice entrypoint — uvicorn-powered WebSocket voice service.

Replaces skvoice on port 18800 (drop-in compatible):
    ws://localhost:18800/ws/voice/{agent}

Environment variables:
    SKCHAT_VOICE_PORT   — listen port (default 18800)
    SKCHAT_VOICE_HOST   — bind host (default 0.0.0.0)
    SKVOICE_*           — all VoiceConfig keys are respected (see voice_engine/config.py)

Operator migration (after validation):
    systemctl --user disable --now skvoice
    systemctl --user enable  --now skchat-voice
"""

from __future__ import annotations

import os


def main() -> None:
    """skchat-voice console-script entry point."""
    import uvicorn  # noqa: PLC0415

    from skchat.transports.websocket import build_app  # noqa: PLC0415

    port = int(os.getenv("SKCHAT_VOICE_PORT", "18800"))
    host = os.getenv("SKCHAT_VOICE_HOST", "0.0.0.0")

    app = build_app()  # uses real VoiceEngine factory from environment

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
