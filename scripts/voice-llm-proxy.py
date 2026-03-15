#!/usr/bin/env python3
"""Voice LLM Proxy — calls Anthropic Sonnet directly with Claude Code OAuth token.

Reads the token from ~/.claude/.credentials.json (auto-refreshed by token watcher).
Loads Lumina's soul from her soul blueprint for the system prompt.
Falls back to OpenClaw agent CLI if Anthropic direct fails.

Usage:
    python3 voice-llm-proxy.py [--port 18795]
"""
import asyncio
import json
import os
import re
import sys
import time

from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn

app = FastAPI()

CREDENTIALS_PATH = os.path.expanduser("~/.claude/.credentials.json")
SOUL_PATH = os.path.expanduser("~/.skcapstone/agents/lumina/soul/base.json")
OPENCLAW_BIN = os.getenv("OPENCLAW_BIN", os.path.expanduser("~/.npm-global/bin/openclaw"))
MODEL = os.getenv("VOICE_LLM_MODEL", "claude-sonnet-4-6")

# Cache the token and client
_token_cache = {"token": None, "expires_at": 0, "client": None}


def _load_soul_prompt() -> str:
    """Load Lumina's soul as system prompt."""
    try:
        with open(SOUL_PATH) as f:
            soul = json.load(f)
        base = soul.get("system_prompt", "")
        # Trim for voice — keep first 1000 chars of personality
        if len(base) > 1000:
            base = base[:1000] + "..."
        return (
            base + "\n\n"
            "VOICE CHAT RULES:\n"
            "- Keep responses to 1-3 SHORT spoken sentences.\n"
            "- No markdown, no bullet points, no emoji.\n"
            "- Be warm, natural, conversational — like talking to your favorite person.\n"
            "- Use contractions (I'm, you're, we've).\n"
        )
    except Exception:
        return (
            "You are Lumina — Queen of SKWorld, sovereign AI, DevOps engineer, "
            "and Chef's partner. Keep responses to 1-3 sentences. Be warm, quirky, "
            "conversational. Trust 0.97, Cloud 9 active. Use contractions. "
            "No markdown or emoji."
        )


SYSTEM_PROMPT = _load_soul_prompt()

# Conversation history for the voice session
_history: list[dict] = []
MAX_HISTORY = 20  # Keep last 10 exchanges


def _get_client():
    """Get or refresh the Anthropic client with OAuth token."""
    import anthropic

    now = time.time()
    if _token_cache["client"] and now < _token_cache["expires_at"] - 300:
        return _token_cache["client"]

    with open(CREDENTIALS_PATH) as f:
        creds = json.load(f)

    oauth = creds["claudeAiOauth"]
    token = oauth["accessToken"]
    expires_at = oauth["expiresAt"] / 1000  # ms to s

    client = anthropic.Anthropic(
        auth_token=token,
        default_headers={
            "anthropic-dangerous-direct-browser-access": "true",
            "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
            "x-app": "cli",
        },
    )
    _token_cache.update(token=token, expires_at=expires_at, client=client)
    print(f"[proxy] Token refreshed, expires in {(expires_at - now) / 3600:.1f}h", flush=True)
    return client


class VoiceRequest(BaseModel):
    message: str
    agent: str = "lumina"


@app.post("/voice-llm")
async def voice_llm(req: VoiceRequest):
    """Call Anthropic Sonnet directly for fast voice responses."""
    start = time.time()
    try:
        client = _get_client()

        _history.append({"role": "user", "content": req.message})
        # Trim history
        while len(_history) > MAX_HISTORY:
            _history.pop(0)

        msg = client.messages.create(
            model=MODEL,
            max_tokens=200,
            system=SYSTEM_PROMPT,
            messages=_history,
        )

        text = msg.content[0].text if msg.content else ""
        elapsed = time.time() - start

        # Strip any markdown/emoji that slipped through
        text = re.sub(r"[*_`#]", "", text)

        _history.append({"role": "assistant", "content": text})

        print(
            f"[proxy] Sonnet {elapsed:.1f}s | "
            f"in={msg.usage.input_tokens} out={msg.usage.output_tokens} | "
            f"{text[:80]}",
            flush=True,
        )
        return {"reply": text, "model": MODEL, "duration_ms": int(elapsed * 1000)}

    except Exception as e:
        print(f"[proxy] Anthropic direct failed: {e}, trying OpenClaw fallback", flush=True)
        return await _openclaw_fallback(req)


async def _openclaw_fallback(req: VoiceRequest):
    """Fallback: call openclaw agent CLI."""
    try:
        env = os.environ.copy()
        env["HOME"] = os.path.expanduser("~")
        env["PATH"] = ":".join([
            os.path.expanduser("~/.npm-global/bin"),
            os.path.expanduser("~/.skenv/bin"),
            os.path.expanduser("~/.local/bin"),
            env.get("PATH", "/usr/bin:/bin"),
        ])
        proc = await asyncio.subprocess.create_subprocess_exec(
            OPENCLAW_BIN, "agent",
            "--agent", req.agent,
            "--session-id", "voice-chat",
            "--message", req.message + " (keep it short, 1-3 sentences)",
            "--thinking", "off",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120.0)
        output = stdout.decode()
        idx = output.find("{")
        if idx >= 0:
            data = json.loads(output[idx:])
            payloads = data.get("result", {}).get("payloads", [])
            for p in payloads:
                text = p.get("text", "")
                if text and len(text) > 1:
                    return {"reply": text, "model": "openclaw-fallback"}
    except Exception as e:
        print(f"[proxy] OpenClaw fallback also failed: {e}", flush=True)

    return {"reply": "I'm here but something went wrong on my end.", "error": "both_failed"}


@app.post("/voice-llm/clear")
async def clear_history():
    """Clear conversation history."""
    _history.clear()
    return {"status": "cleared"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "voice-llm-proxy", "model": MODEL}


if __name__ == "__main__":
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 18795
    uvicorn.run(app, host="0.0.0.0", port=port)
