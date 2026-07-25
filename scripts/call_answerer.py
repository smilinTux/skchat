#!/usr/bin/env python3
"""Runnable entry for the 1:1 auto-answer service.

Thin wrapper over ``skchat.call_answerer`` (the tested core lives in the package
so it is importable without pulling in LiveKit). Run as the callee agent:

    SKAGENT=opus SKCHAT_WEBUI_URL=http://localhost:8765 \
    SKCHAT_GUEST_OPERATOR_TOKEN=... python scripts/call_answerer.py
"""
from skchat.call_answerer import main

if __name__ == "__main__":
    raise SystemExit(main())
