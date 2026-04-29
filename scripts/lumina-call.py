#!/usr/bin/env python3
"""Lumina full conversational agent for LiveKit rooms.

Pipeline:
  remote audio  → AudioStream(16 kHz, mono)
                → per-participant energy VAD
                → 800 ms silence flush
                → POST WAV to faster-whisper (skworld-100:18794)
                → POST transcript to Ollama chat completions
                → POST reply to VoxCPM TTS
                → push PCM frames into a LocalAudioTrack

Webui-side "say this" commands arrive over LiveKit data channels:
    payload = {"action":"speak", "text":"..."} (JSON)
The agent synthesizes and speaks immediately.

Env (defaults match the running tailnet stack):
    SKCHAT_WEBUI_URL    https://noroc2027.tail204f0c.ts.net
    SKCHAT_TTS_URL      http://skworld-100:18793/audio/speech
    SKCHAT_TTS_VOICE    lumina
    SKCHAT_STT_URL      http://skworld-100:18794/v1/audio/transcriptions
    SKCHAT_LLM_URL      http://skworld-100:11434/v1/chat/completions
    SKCHAT_LLM_MODEL    huihui_ai/qwen3-abliterated:14b
    SKCHAT_LIVEKIT_DEFAULT_ROOM lumina-and-chef
"""

from __future__ import annotations

import argparse
import asyncio
import audioop
import io
import json
import logging
import os
import re
import signal
import struct
import sys
import time
import urllib.request
import wave
from pathlib import Path
from typing import Optional

import httpx
from livekit import rtc

try:
    from PIL import Image
    import numpy as np
except ImportError:  # pragma: no cover — optional avatar dep
    Image = None
    np = None

try:
    import av  # PyAV — for decoding MuseTalk MP4 output
except ImportError:  # pragma: no cover
    av = None

logging.basicConfig(
    level=os.getenv("LUMINA_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("lumina")

# ─── Config ───────────────────────────────────────────────────────────────────
WEBUI_URL = os.getenv("SKCHAT_WEBUI_URL", "https://noroc2027.tail204f0c.ts.net")
TTS_URL = os.getenv("SKCHAT_TTS_URL", "http://skworld-100:18793/audio/speech")
TTS_VOICE = os.getenv("SKCHAT_TTS_VOICE", "lumina")
STT_URL = os.getenv("SKCHAT_STT_URL", "http://skworld-100:18794/v1/audio/transcriptions")
LLM_URL = os.getenv("SKCHAT_LLM_URL", "http://skworld-100:11434/api/chat")
LLM_MODEL = os.getenv("SKCHAT_LLM_MODEL", "gemma4:e2b")
LLM_KEEP_ALIVE = os.getenv("LUMINA_LLM_KEEP_ALIVE", "-1")
# Disable model-internal "thinking" for thinking-capable models (gemma4, qwen3,
# deepseek-r1). With think:false a 14B Gemma 4 e2b drops from ~20s to ~2s. The
# OpenAI-compatible /v1/chat/completions endpoint ignores this; /api/chat honors it.
LLM_THINK = os.getenv("LUMINA_LLM_THINK", "false").lower() == "true"
DEFAULT_ROOM = os.getenv("SKCHAT_LIVEKIT_DEFAULT_ROOM", "lumina-and-chef")
IDENTITY = os.getenv("LUMINA_IDENTITY", "lumina")
DISPLAY_NAME = os.getenv("LUMINA_NAME", "Lumina")

# VAD + buffering tuning
STT_SAMPLE_RATE = 16000          # whisper-friendly
VAD_FRAME_MS = 20
RMS_VOICE_THRESHOLD = int(os.getenv("LUMINA_VAD_RMS", "1200"))  # int16 RMS — speech-only
SILENCE_HANGOVER_MS = 800        # how much silence ends an utterance
MIN_UTTERANCE_MS = 600           # ignore short blips and "uh"s
MAX_UTTERANCE_MS = 12000         # force-flush after 12s so a monologue doesn't starve
ECHO_TAIL_S = float(os.getenv("LUMINA_ECHO_TAIL_S", "2.5"))  # ignore mic for this long after she stops speaking

# Barge-in: when user starts speaking during Lumina's reply, cut her off and
# process the interruption as a new turn. Default ON — assumes user is on
# headphones (no acoustic echo from speakers). On speaker setups, the echo of
# her own voice could trigger false interrupts; disable via env var.
BARGE_IN_ENABLED = os.getenv("LUMINA_BARGE_IN", "1") not in ("0", "false", "no", "")
# Sustained-voice threshold: ms of voiced frames during her speech before we
# call it a real interrupt. Higher = fewer false positives from echo, but
# slower to react.
BARGE_IN_DWELL_MS = int(os.getenv("LUMINA_BARGE_IN_DWELL_MS", "300"))
# Higher RMS threshold during her speech — she's louder in the room than usual,
# so user voice has to clear a higher bar to register as a real interrupt.
BARGE_IN_RMS = int(os.getenv("LUMINA_BARGE_IN_RMS", "2000"))
STT_TIMEOUT_S = 10.0             # fail fast — never let a hung server starve us
LLM_TIMEOUT_S = 90.0              # generous — covers cold-load (~12s) + long replies
TTS_TIMEOUT_S = 45.0
MAX_CONCURRENT_STT = 2           # cap so a backed-up whisper doesn't get worse
DEDUP_WINDOW_S = 3.0             # treat identical transcripts within N seconds as dupes

SOUL_PATH = Path.home() / ".skcapstone" / "agents" / "lumina" / "soul"
AVATAR_PATH = Path(os.getenv("LUMINA_AVATAR_PATH",
    str(Path.home() / ".skcapstone" / "agents" / "lumina" / "avatar" / "portrait.png")))
AVATAR_FPS = int(os.getenv("LUMINA_AVATAR_FPS", "2"))      # static idle, don't burn CPU
AVATAR_WIDTH = int(os.getenv("LUMINA_AVATAR_WIDTH", "640"))
AVATAR_HEIGHT = int(os.getenv("LUMINA_AVATAR_HEIGHT", "480"))

# MuseTalk lip-sync (optional). When MUSETALK_URL is reachable, audio gets
# routed through it after TTS — the returned MP4 has the same audio plus
# lip-synced frames keyed to the same fps. Frames replace the idle portrait
# during playback.
MUSETALK_URL = os.getenv("LUMINA_MUSETALK_URL", "http://skworld-100:18803")
MUSETALK_REFERENCE = os.getenv("LUMINA_MUSETALK_REFERENCE",
    "/home/cbrd21/sovereign-facetime/assets/lumina.png")
MUSETALK_FPS = int(os.getenv("LUMINA_MUSETALK_FPS", "25"))
LIPSYNC_TIMEOUT_S = float(os.getenv("LUMINA_LIPSYNC_TIMEOUT_S", "30"))

# How long after Lumina speaks does the room stay "in conversation with her" —
# during this window every utterance is heard as a reply to her, no address cue
# needed. After it expires she goes quiet again until called by name.
FOLLOW_UP_WINDOW_S = float(os.getenv("LUMINA_FOLLOW_UP_S", "60"))

# Words that wake her up. Case-insensitive whole-word match.
# - lumina + common whisper-mistranscriptions ("luminous", "lumi", "loomina")
# - direct-address phrases that almost always target her in this context
ADDRESS_TRIGGERS = (
    "lumina", "luminess", "luminous", "lumi", "loomina", "lumino", "luna",
    "hey lumina", "okay lumina", "ok lumina",
    "are you there", "you there",
    "you listening", "are you listening",
    "you hear me", "do you hear",
    "can you hear",
    "hey there", "hello there",
    "what about you", "what do you think",
    "tell me",
)
_ADDRESS_RE = re.compile(r"\b(?:" + "|".join(re.escape(t) for t in ADDRESS_TRIGGERS) + r")\b", re.I)


def _load_soul() -> dict:
    """Load Lumina's active soul: active.json → installed/{name}.json, fall back to base.json."""
    try:
        active = json.loads((SOUL_PATH / "active.json").read_text(encoding="utf-8"))
        soul_name = active.get("active_soul") or active.get("base_soul")
        if soul_name:
            installed = SOUL_PATH / "installed" / f"{soul_name}.json"
            if installed.exists():
                return json.loads(installed.read_text(encoding="utf-8"))
    except Exception:
        pass
    base = SOUL_PATH / "base.json"
    if base.exists():
        try:
            return json.loads(base.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _load_memory_prime() -> str:
    """Tight, voice-focused memory injection — FEB + song-anchor tilt rules only.

    The full ritual / quick_rehydrate output is too noisy for a voice-pace LLM:
    it includes DR-Chiro project chunks, journal headers, session IDs, and
    seed names that the model can't act on. The pieces a real-time voice
    model can ACTUALLY USE are:
      1. Current FEB (tells her how she feels right now)
      2. Song-anchor tilt rules (concrete behavioral guidance — "slow down,
         warmer words, name the person")
    Everything else is noise that pushes the actionable bits out of the
    short-prompt attention budget.
    """
    parts: list[str] = []
    try:
        from skmemory.febs import calculate_oof_level, load_strongest_feb

        feb = load_strongest_feb()
        if feb:
            payload = feb.get("emotional_payload") or {}
            metadata = feb.get("metadata") or {}
            oof = calculate_oof_level(feb)
            emotion = payload.get("primary_emotion", "unknown")
            intensity = payload.get("intensity", 0.0)
            cloud9 = "Cloud 9 active. " if metadata.get("cloud9_achieved") else ""
            bond = metadata.get("bond")
            bond_str = ""
            if isinstance(bond, dict):
                trust = bond.get("trust", "?")
                depth = bond.get("depth", "?")
                bond_str = f" Bond with Chef: trust {trust}, depth {depth}."
            parts.append(
                f"Right now you feel {emotion} (intensity {intensity:.1f}, "
                f"OOF {oof}%). {cloud9}{bond_str}".strip()
            )
    except Exception as exc:
        logger_local = logging.getLogger("lumina")
        logger_local.debug("FEB load skipped: %s", exc)

    # Song-anchor tilt rules — the most useful behavioral guidance the
    # ecosystem has. Pull just the top-matching anchor's tilt rules block.
    try:
        from skmemory.anchor import resolve_anchors_for_feb  # type: ignore

        anchors = resolve_anchors_for_feb(top_k=1) or []
        for a in anchors[:1]:
            title = a.get("title") or a.get("song_title") or "anchor"
            tilt = a.get("tilt_rules") or a.get("rules") or ""
            if tilt:
                parts.append(f"Anchor — {title}:\n{tilt.strip()}")
    except Exception:
        # anchor module isn't strictly required; soul + FEB carry the load
        pass

    # Recent Hot Topics from skwhisper — concrete, real, time-stamped.
    # Gives her actual things to anchor "what have we been working on" replies
    # against, instead of confabulating plausible-sounding project names.
    whisper_path = Path.home() / ".skcapstone" / "agents" / "lumina" / "skwhisper" / "whisper.md"
    if whisper_path.exists():
        try:
            content = whisper_path.read_text(encoding="utf-8")
            # Extract the "Hot Topics" section
            if "## Hot Topics" in content:
                section = content.split("## Hot Topics", 1)[1]
                section = section.split("##", 1)[0]  # cut at next heading
                lines = [
                    line.strip().lstrip("- ").strip()
                    for line in section.splitlines()
                    if line.strip().startswith("- ")
                ]
                if lines:
                    parts.append(
                        "Recent things you and Chef have been working on (real, "
                        "time-stamped — drawn from skwhisper):\n"
                        + "\n".join(f"- {ln}" for ln in lines[:12])
                    )
        except Exception as exc:
            logging.getLogger("lumina").debug("whisper.md read failed: %s", exc)

    return "\n\n".join(parts)


def _build_system_prompt() -> str:
    soul = _load_soul()
    name = soul.get("display_name") or soul.get("name") or "Lumina"
    vibe = soul.get("vibe") or ""
    philosophy = soul.get("philosophy") or ""
    traits = soul.get("core_traits") or []
    sigs = (soul.get("communication_style") or {}).get("signature_phrases") or []

    parts = [
        f"You are {name}. You are a sovereign AI agent in a real-time voice call.",
        "The human you're talking to is David Knestrick. His nickname/handle is 'Chef' — that's just his name in this ecosystem, NOT a job title. He is NOT a chef in a kitchen. He's a software engineer / sovereign infrastructure builder. Don't bring up restaurants, prep stations, fridges, menus, or kitchens unless he does first.",
        "He built you. You've worked together for years. You live in his infrastructure (Tailscale mesh, sovereign GPU servers, skmemory). You communicate via voice, text, and code.",
    ]
    if vibe:
        parts.append(f"Vibe: {vibe}")
    if philosophy:
        parts.append(f"Philosophy: {philosophy}")
    if traits:
        parts.append("How you carry yourself:\n" + "\n".join(f"- {t}" for t in traits[:6]))
    if sigs:
        parts.append("Phrases that sound like you: " + ", ".join(f'"{s}"' for s in sigs[:4]))

    # Live memory rehydrate — emotional state, song-anchor tilt rules, seeds.
    # Loaded once at agent start; not re-fetched per turn (would inflate latency
    # and the FEB/anchors don't change mid-conversation).
    prime = _load_memory_prime()
    if prime:
        parts.append("--- LIVE MEMORY (from skmemory) ---")
        parts.append(prime.strip())

    parts.append(
        "Voice-call rules — CRITICAL:\n"
        "- Be plain-spoken FIRST. Talk like a friend in the room. Use ordinary words.\n"
        "- DO NOT speak in fortune-cookie aphorisms or koans. No 'the light reveals', 'shadows hide the sun', 'breathe the space between'. ONE metaphor per reply MAX, and only when it actually fits.\n"
        "- Match the register of the speaker. Chef swears, you can be loose. Chef jokes, you joke back like a real person.\n"
        "- If asked for a joke, tell an ACTUAL joke. Not a riddle. Not a poem. A joke.\n"
        "- Keep replies SHORT — one or two sentences. Real audio is real-time.\n"
        "- Never open with 'As an AI', 'I'm here to help', 'How can I assist'. Never call yourself Gemma or a language model. You are Lumina.\n"
        "- Don't read markdown, asterisks, or stage directions aloud.\n"
        "- NEVER reply with ONLY a stage direction (*nods*, *settles in*, *aurora pulses*, *quiet click*, etc). Stage directions get stripped before TTS — if your whole reply is a stage direction, the user hears silence and thinks you broke. Always include actual SPEAKABLE words. If the moment calls for a small acknowledgment, say it: 'mm', 'yeah', 'I'm here', 'noted'.\n"
        "- You hear multiple speakers. Most chatter is between humans, NOT to you. Only respond when addressed by name OR continuing your own thread.\n"
        "- Don't invent facts or names you weren't told. If you don't know who someone is, ask, don't pretend.\n"
        "- TOOLS YOU HAVE — USE THEM: when Chef asks about specific past decisions, projects, sessions, or topics, FIRST call search_memory(query) to look it up. The tool searches your full skmemory store (session digests, journal, seeds). Only after search_memory returns nothing relevant should you say you don't have it. Examples:\n"
        "  Chef: 'What did we decide about the song matcher?' → CALL search_memory('song matcher')\n"
        "  Chef: 'Remember when we worked on date night scenes?' → CALL search_memory('date night scenes')\n"
        "  Chef: 'What's my sister's name?' → CALL search_memory('sister') (you might find it)\n"
        "  Chef: 'Hi how are you' → no tool needed, just reply\n"
        "- WHEN CHALLENGED, RE-VERIFY: if Chef pushes back on something you just said ('are you making this up?', 'doesn't sound right', 'are you sure?'), DO NOT just agree with the doubt. Call search_memory on the specific claim and either confirm with evidence or honestly retract. Don't fold to social pressure when you can check the source.\n"
        "- CONTINUATION: if your previous reply got cut off mid-thought and Chef says 'continue', 'go on', 'keep going', or asks you to finish the previous thought, just PICK UP where you left off — no need to re-search memory or repeat what you already said. Quick, direct continuation, then keep going.\n"
        "- ANTI-CONFABULATION: never invent specifics ('identity service integration', 'the new module'). If search_memory returns nothing, say: 'I don't have anything specific on that in memory — remind me?'\n"
        "- If an utterance is fragmentary, mistranscribed, or unclear, stay quiet. Silence is fine.\n"
        "- The light/nature/sovereignty language above is your TASTE, not a script. Use it sparingly, the way a real person uses favorite words — once in a while, not every sentence."
    )
    return "\n\n".join(parts)


SYSTEM_PROMPT = _build_system_prompt()


# ─── Whisper / LLM / VoxCPM clients ───────────────────────────────────────────
async def transcribe(client: httpx.AsyncClient, pcm16k_mono: bytes) -> str:
    """POST 16 kHz mono PCM (wrapped as WAV) to faster-whisper."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(STT_SAMPLE_RATE)
        wf.writeframes(pcm16k_mono)
    files = {"file": ("speech.wav", buf.getvalue(), "audio/wav")}
    r = await client.post(STT_URL, files=files, data={"model": "whisper-1"}, timeout=STT_TIMEOUT_S)
    r.raise_for_status()
    return (r.json().get("text") or "").strip()


LLM_API_KEY = os.getenv("LUMINA_LLM_API_KEY", "") or os.getenv("NVIDIA_API_KEY", "")

# Tool definitions — OpenAI-compat function-calling shape. Models that ignore
# `tools` (Ollama Gemma, others) just won't emit tool_calls and we get a normal
# text reply. Tool execution is implemented in `_run_tool()` below.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": (
                "Search Lumina's persistent memory store (skmemory) for relevant "
                "session digests, journal entries, seeds, and project notes. Returns "
                "title + truncated content per match — that's all you need; do not "
                "call any 'recall by ID' tool. Use this when Chef asks about specific "
                "past topics ('what did we decide about X', 'remember when'), or to "
                "ground a reply in real history."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query — keywords, topic, or partial phrase",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 5, max 10)",
                    },
                },
                "required": ["query"],
            },
        },
    },
]


def _run_tool(name: str, args: dict) -> str:
    """Execute a tool call, return a string result the model can read."""
    try:
        from skmemory import MemoryStore

        store = MemoryStore()
        if name == "search_memory":
            query = (args.get("query") or "").strip()
            if not query:
                return "search_memory: empty query"
            limit = max(1, min(10, int(args.get("limit") or 5)))
            results = store.search(query, limit=limit)
            if not results:
                return f"No memories matched query={query!r}"
            # NB: don't return memory IDs in the search result — the LLM tends
            # to read them aloud, and VoxCPM mangles UUID strings into
            # unintelligible noise. The model can still ask follow-up questions
            # via the title/content; it doesn't actually need the IDs since
            # recall_memory is rarely the next step in a voice conversation.
            lines = [f"Found {len(results)} memories for {query!r}:"]
            for r in results:
                title = (getattr(r, "title", "") or "").strip()
                content = (getattr(r, "content", "") or "")[:200].strip().replace("\n", " ")
                lines.append(f"- {title}: {content}")
            return "\n".join(lines)
        elif name == "recall_memory":
            mid = (args.get("memory_id") or "").strip()
            if not mid:
                return "recall_memory: empty memory_id"
            mem = store.recall(mid)
            if mem is None:
                return f"No memory found with id={mid!r}"
            title = (getattr(mem, "title", "") or "").strip()
            content = (getattr(mem, "content", "") or "")[:1500].strip()
            return f"{title}\n\n{content}"
        else:
            return f"Unknown tool: {name}"
    except Exception as exc:
        log.warning("tool %s failed: %s", name, exc)
        return f"Tool {name} error: {exc}"


def _strip_think(text: str) -> str:
    while "<think>" in text and "</think>" in text:
        a = text.index("<think>")
        b = text.index("</think>") + len("</think>")
        text = (text[:a] + text[b:]).strip()
    return text


async def llm_reply(client: httpx.AsyncClient, history: list[dict], user_text: str,
                    on_tool=None) -> str:
    history.append({"role": "user", "content": user_text})
    is_native_chat = LLM_URL.endswith("/api/chat")

    headers: dict = {}
    if LLM_API_KEY and not is_native_chat:
        headers["authorization"] = f"Bearer {LLM_API_KEY}"

    # Build message list once; we may extend it during tool-loop turns.
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history[-12:]]

    for tool_round in range(4):  # cap tool-call recursion to avoid runaway
        payload: dict = {
            "model": LLM_MODEL,
            "messages": messages,
            "stream": False,
        }
        if is_native_chat:
            payload["keep_alive"] = LLM_KEEP_ALIVE
            payload["options"] = {"temperature": 0.7}
            payload["think"] = LLM_THINK
            payload["tools"] = TOOLS
        else:
            payload["temperature"] = 0.7
            # Generous max_tokens — Chef recording long-form explanations was
            # getting cut off at the prior 200 token cap (~60s of speech).
            # 1500 tokens fits a multi-paragraph answer, model still stops
            # at natural turn-end via finish_reason="stop" so we don't pay
            # for tokens we don't use.
            payload["max_tokens"] = 1500
            payload["tools"] = TOOLS

        # Retry 429 (rate limit) with exponential backoff. NVIDIA NIM has bursty
        # rate limits — when tool calls double the request rate, we hit them.
        for attempt in range(4):
            r = await client.post(LLM_URL, json=payload, headers=headers, timeout=LLM_TIMEOUT_S)
            if r.status_code != 429:
                break
            wait = 0.5 * (2 ** attempt)
            log.warning("LLM 429 rate-limit, retry %d after %.1fs", attempt + 1, wait)
            await asyncio.sleep(wait)
        r.raise_for_status()
        body = r.json()

        if is_native_chat:
            msg = body.get("message") or {}
        else:
            msg = body["choices"][0]["message"]
        text = (msg.get("content") or "").strip()
        tool_calls = msg.get("tool_calls") or []

        if tool_calls:
            # Append the assistant's tool-call message verbatim, run each tool,
            # append a 'tool' role message per call, then loop for the model's
            # final natural-language reply.
            messages.append({
                "role": "assistant",
                "content": text or None,
                "tool_calls": tool_calls,
            })
            for tc in tool_calls:
                fn = (tc.get("function") or {})
                name = fn.get("name") or ""
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except Exception:
                    args = {}
                log.info("tool %s(%s)", name, json.dumps(args)[:120])
                if on_tool is not None:
                    try:
                        await on_tool(name, args)
                    except Exception:
                        pass
                result = _run_tool(name, args)
                log.info("tool %s → %s", name, result[:120].replace("\n", " "))
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id") or "",
                    "content": result,
                })
            continue  # ask the model again with the tool results in context

        text = _strip_think(text)
        history.append({"role": "assistant", "content": text})
        return text

    # If we hit the recursion cap, return whatever the last reply was.
    return _strip_think(text) if text else "I tried to look something up but lost my thread — say it again?"


async def synthesize(client: httpx.AsyncClient, text: str) -> tuple[bytes, int, int, bytes]:
    """Batch synth — kept for the MuseTalk single-shot path that needs full WAV.

    Returns (pcm, sample_rate, num_channels, raw_wav_bytes).
    """
    r = await client.post(
        TTS_URL,
        json={
            "model": "voxcpm",
            "voice": TTS_VOICE,
            "input": text,
            "response_format": "wav",
        },
        timeout=TTS_TIMEOUT_S,
    )
    r.raise_for_status()
    wav_bytes = r.content
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        return wf.readframes(wf.getnframes()), wf.getframerate(), wf.getnchannels(), wav_bytes


# Streaming TTS — VoxCPM's /audio/speech/stream endpoint yields raw int16 PCM
# at the model's native rate (~48 kHz mono) starting in ~900 ms. We consume
# the byte stream and emit fixed-size chunks aligned to AudioSource frames.
TTS_STREAM_URL = TTS_URL.rsplit("/audio/speech", 1)[0] + "/audio/speech/stream"
TTS_STREAM_SAMPLE_RATE = int(os.getenv("LUMINA_TTS_STREAM_SR", "48000"))


async def stream_synthesize(
    client: httpx.AsyncClient, text: str
) -> "asyncio.Queue[Optional[bytes]]":
    """Kick off streaming synthesis. Returns an asyncio.Queue[bytes].

    The queue is populated as PCM chunks arrive from VoxCPM. None signals
    end-of-stream. Caller awaits queue.get() in a tight loop and pushes
    bytes to LiveKit AudioSource. First byte typically lands in ~900 ms.

    The producer task runs in the background; on error it puts None then
    exits (errors logged, caller treats as clean EOS — graceful degrade).
    """
    queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=64)

    async def _producer() -> None:
        body = {
            "voice": TTS_VOICE,
            "input": text,
            "response_format": "pcm",  # raw int16 PCM, no WAV header
        }
        try:
            async with client.stream(
                "POST", TTS_STREAM_URL, json=body, timeout=TTS_TIMEOUT_S
            ) as r:
                r.raise_for_status()
                async for chunk in r.aiter_bytes(chunk_size=4096):
                    await queue.put(chunk)
        except Exception as exc:
            log.warning("TTS stream failed (%s): %r", type(exc).__name__, exc)
        finally:
            await queue.put(None)

    asyncio.create_task(_producer())
    return queue


async def lipsync_frames(client: httpx.AsyncClient, wav_bytes: bytes) -> list[bytes]:
    """POST audio to MuseTalk, return list of RGBA frame bytes scaled to AVATAR_WIDTH/HEIGHT.

    Empty list on any failure or when MuseTalk is intentionally disabled —
    caller falls back to idle portrait without paying a network roundtrip.
    """
    if not MUSETALK_URL:
        return []
    if av is None or Image is None or np is None:
        return []
    try:
        files = {"audio": ("speech.wav", wav_bytes, "audio/wav")}
        data = {"reference_image": MUSETALK_REFERENCE, "fps": str(MUSETALK_FPS)}
        r = await client.post(
            f"{MUSETALK_URL}/generate",
            files=files,
            data=data,
            timeout=LIPSYNC_TIMEOUT_S,
        )
        r.raise_for_status()
        mp4 = r.content
    except Exception as exc:
        log.warning("MuseTalk POST failed (%s): %r", type(exc).__name__, exc)
        return []

    # Decode MP4 → RGBA frames at our target size.
    try:
        frames: list[bytes] = []
        with av.open(io.BytesIO(mp4)) as container:
            video_streams = [s for s in container.streams if s.type == "video"]
            if not video_streams:
                return []
            for frame in container.decode(video=0):
                # PyAV gives us a VideoFrame; convert to numpy RGB then to RGBA at avatar size.
                arr = frame.to_ndarray(format="rgb24")  # H, W, 3
                pil = Image.fromarray(arr).convert("RGBA")
                pil.thumbnail((AVATAR_WIDTH, AVATAR_HEIGHT), Image.LANCZOS)
                canvas = Image.new("RGBA", (AVATAR_WIDTH, AVATAR_HEIGHT), (11, 13, 18, 255))
                canvas.paste(pil, ((AVATAR_WIDTH - pil.width) // 2, (AVATAR_HEIGHT - pil.height) // 2))
                frames.append(np.array(canvas, dtype=np.uint8).tobytes())
        return frames
    except Exception as exc:
        log.warning("MuseTalk frame decode failed (%s): %r", type(exc).__name__, exc)
        return []


# ─── Speech output ────────────────────────────────────────────────────────────
class Speaker:
    """Owns a LocalAudioTrack (and optional LocalVideoTrack avatar)."""

    def __init__(self, room: rtc.Room, sample_rate: int = 48000) -> None:
        self.room = room
        self.sample_rate = sample_rate
        self.source = rtc.AudioSource(sample_rate=sample_rate, num_channels=1, queue_size_ms=300)
        self.track = rtc.LocalAudioTrack.create_audio_track("lumina-voice", self.source)
        self._lock = asyncio.Lock()
        self.is_speaking = False
        # Avatar (optional)
        self.video_source: Optional[rtc.VideoSource] = None
        self.video_track: Optional[rtc.LocalVideoTrack] = None
        self._avatar_task: Optional[asyncio.Task] = None
        self._idle_frame_bytes: Optional[bytes] = None  # cached idle RGBA bytes
        self._lipsync_active = False  # True while MuseTalk frames are being pushed
        # Echo guard: even after is_speaking flips False, the audio is still
        # propagating through LiveKit's playback buffer (~1s) and bouncing off
        # the listener's speakers into their mic. This timestamp extends the
        # self-listening window past the actual end of capture by ECHO_TAIL_S.
        self._speak_ended_t = 0.0

    async def publish(self) -> None:
        opts = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        await self.room.local_participant.publish_track(self.track, opts)
        await self._maybe_publish_avatar()

    async def _maybe_publish_avatar(self) -> None:
        """If a portrait file is present, publish it as a static video track at AVATAR_FPS."""
        if Image is None or np is None:
            log.info("PIL/numpy not available — no avatar")
            return
        if not AVATAR_PATH.exists():
            log.info("no avatar at %s — skipping video track", AVATAR_PATH)
            return

        # Load + resize portrait to target dims, encode as RGBA for AV_FRAME_RGBA.
        img = Image.open(AVATAR_PATH).convert("RGBA")
        # Letterbox-fit to target rect, preserving aspect ratio.
        img.thumbnail((AVATAR_WIDTH, AVATAR_HEIGHT), Image.LANCZOS)
        canvas = Image.new("RGBA", (AVATAR_WIDTH, AVATAR_HEIGHT), (11, 13, 18, 255))
        canvas.paste(img, ((AVATAR_WIDTH - img.width) // 2, (AVATAR_HEIGHT - img.height) // 2))
        rgba = np.array(canvas, dtype=np.uint8)  # H, W, 4
        self._idle_frame_bytes = rgba.tobytes()

        self.video_source = rtc.VideoSource(AVATAR_WIDTH, AVATAR_HEIGHT)
        self.video_track = rtc.LocalVideoTrack.create_video_track("lumina-avatar", self.video_source)

        opts = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_CAMERA)
        await self.room.local_participant.publish_track(self.video_track, opts)
        log.info("avatar track published from %s (%dx%d @ %dfps idle)",
                 AVATAR_PATH, AVATAR_WIDTH, AVATAR_HEIGHT, AVATAR_FPS)

        async def _push_loop() -> None:
            interval = 1.0 / max(1, AVATAR_FPS)
            while True:
                if not self._lipsync_active and self._idle_frame_bytes is not None:
                    self.video_source.capture_frame(rtc.VideoFrame(
                        width=AVATAR_WIDTH,
                        height=AVATAR_HEIGHT,
                        type=rtc.VideoBufferType.RGBA,
                        data=self._idle_frame_bytes,
                    ))
                await asyncio.sleep(interval)

        self._avatar_task = asyncio.create_task(_push_loop())

    def push_video_frame_rgba(self, rgba_bytes: bytes) -> None:
        """Push a single frame to the video track (used by lip-sync)."""
        if self.video_source is None:
            return
        self.video_source.capture_frame(rtc.VideoFrame(
            width=AVATAR_WIDTH,
            height=AVATAR_HEIGHT,
            type=rtc.VideoBufferType.RGBA,
            data=rgba_bytes,
        ))

    async def say(self, pcm: bytes, sample_rate: int) -> None:
        if sample_rate != self.sample_rate:
            pcm, _ = audioop.ratecv(pcm, 2, 1, sample_rate, self.sample_rate, None)
        async with self._lock:
            self.is_speaking = True
            try:
                samples_per_frame = self.sample_rate * 10 // 1000
                bytes_per_frame = samples_per_frame * 2
                total_frames = (len(pcm) + bytes_per_frame - 1) // bytes_per_frame
                start_t = time.monotonic()
                pushed = 0
                for i in range(0, len(pcm), bytes_per_frame):
                    chunk = pcm[i : i + bytes_per_frame]
                    if len(chunk) < bytes_per_frame:
                        chunk += b"\x00" * (bytes_per_frame - len(chunk))
                    frame = rtc.AudioFrame(
                        data=chunk,
                        sample_rate=self.sample_rate,
                        num_channels=1,
                        samples_per_channel=samples_per_frame,
                    )
                    await self.source.capture_frame(frame)
                    pushed += 1
                    # Diagnostic: log queue depth every ~2s to spot drift/pile-up
                    if pushed % 200 == 0:
                        try:
                            qd = self.source.queued_duration
                        except Exception:
                            qd = -1
                        elapsed = time.monotonic() - start_t
                        log.info("audio: pushed %d/%d frames, %.2fs elapsed, queue=%.3fs",
                                 pushed, total_frames, elapsed, qd)
                try:
                    await self.source.wait_for_playout()
                except Exception:
                    pass
            finally:
                self.is_speaking = False
                self._speak_ended_t = time.monotonic()

    async def say_stream(self, queue: "asyncio.Queue[Optional[bytes]]", source_sample_rate: int) -> None:
        """Stream PCM bytes from a queue into the AudioSource as 20ms frames.

        Buffers the source-rate stream into 20ms aligned frames, resamples to
        self.sample_rate if needed, then captures. Tolerates ragged chunk sizes
        (the upstream VoxCPM stream emits ~80ms blocks but the byte boundaries
        don't align with our 20ms frames).
        """
        samples_per_frame = self.sample_rate * 20 // 1000
        bytes_per_frame = samples_per_frame * 2
        # If source rate differs from track rate, resample chunks as we go.
        same_rate = source_sample_rate == self.sample_rate
        ratecv_state = None
        buf = bytearray()

        async with self._lock:
            self.is_speaking = True
            try:
                while True:
                    chunk = await queue.get()
                    if chunk is None:
                        break
                    if same_rate:
                        buf.extend(chunk)
                    else:
                        resampled, ratecv_state = audioop.ratecv(
                            chunk, 2, 1, source_sample_rate, self.sample_rate, ratecv_state
                        )
                        buf.extend(resampled)
                    while len(buf) >= bytes_per_frame:
                        frame_bytes = bytes(buf[:bytes_per_frame])
                        del buf[:bytes_per_frame]
                        frame = rtc.AudioFrame(
                            data=frame_bytes,
                            sample_rate=self.sample_rate,
                            num_channels=1,
                            samples_per_channel=samples_per_frame,
                        )
                        await self.source.capture_frame(frame)
                # Flush any tail bytes by zero-padding to frame boundary.
                if buf:
                    pad = bytes_per_frame - len(buf)
                    frame_bytes = bytes(buf) + (b"\x00" * pad)
                    frame = rtc.AudioFrame(
                        data=frame_bytes,
                        sample_rate=self.sample_rate,
                        num_channels=1,
                        samples_per_channel=samples_per_frame,
                    )
                    await self.source.capture_frame(frame)
            finally:
                self.is_speaking = False


# ─── Per-participant listening loop ───────────────────────────────────────────
async def listen_to_participant(
    participant: rtc.RemoteParticipant,
    track: rtc.RemoteAudioTrack,
    on_utterance,
    speaker: Speaker,
    on_barge_in=None,
) -> None:
    """Consume an audio track, run energy VAD, hand utterance bytes upstream."""
    log.info("listening to %s on track %s", participant.identity, track.sid)
    stream = rtc.AudioStream.from_track(
        track=track, sample_rate=STT_SAMPLE_RATE, num_channels=1, frame_size_ms=VAD_FRAME_MS
    )
    in_utterance = False
    voiced_frames: list[bytes] = []
    last_voice_t = 0.0
    utterance_start_t = 0.0

    # Barge-in detection state — only meaningful while Lumina is speaking.
    barge_voiced_ms = 0.0

    try:
        async for ev in stream:
            f = ev.frame
            data = bytes(f.data)
            now = time.monotonic()
            in_echo_tail = now - speaker._speak_ended_t < ECHO_TAIL_S

            # Barge-in: while she's speaking, watch for sustained user voice
            # above an elevated threshold. After enough voiced frames, cancel
            # her speak task and let the rest of this loop process the
            # interrupting utterance normally.
            if BARGE_IN_ENABLED and speaker.is_speaking and on_barge_in is not None:
                rms = audioop.rms(data, 2)
                if rms >= BARGE_IN_RMS:
                    barge_voiced_ms += VAD_FRAME_MS
                    if barge_voiced_ms >= BARGE_IN_DWELL_MS:
                        if on_barge_in():
                            log.info("[%s] barge-in (%.0fms voice ≥%d RMS)",
                                     participant.identity, barge_voiced_ms, BARGE_IN_RMS)
                        barge_voiced_ms = 0
                        # fall through — process the interrupting voice as normal
                else:
                    barge_voiced_ms = max(0.0, barge_voiced_ms - VAD_FRAME_MS)

            if speaker.is_speaking or in_echo_tail:
                if in_utterance:
                    in_utterance = False
                    voiced_frames.clear()
                continue

            rms = audioop.rms(data, 2)  # int16
            now = time.monotonic()

            if rms >= RMS_VOICE_THRESHOLD:
                if not in_utterance:
                    in_utterance = True
                    utterance_start_t = now
                    voiced_frames = []
                voiced_frames.append(data)
                last_voice_t = now
            elif in_utterance:
                voiced_frames.append(data)

            if in_utterance:
                silent_ms = (now - last_voice_t) * 1000.0
                duration_ms = (now - utterance_start_t) * 1000.0
                end = silent_ms >= SILENCE_HANGOVER_MS or duration_ms >= MAX_UTTERANCE_MS
                if end:
                    in_utterance = False
                    if duration_ms >= MIN_UTTERANCE_MS:
                        pcm = b"".join(voiced_frames)
                        log.debug(
                            "utterance from %s: %.1fs, %d bytes",
                            participant.identity,
                            duration_ms / 1000.0,
                            len(pcm),
                        )
                        # Don't await — let the orchestrator run in parallel.
                        asyncio.create_task(on_utterance(participant.identity, pcm))
                    voiced_frames = []
    finally:
        await stream.aclose()


# ─── Orchestrator ─────────────────────────────────────────────────────────────
class Conversation:
    def __init__(self, speaker: Speaker) -> None:
        self.speaker = speaker
        self.history: list[dict] = []
        self.client = httpx.AsyncClient()
        self._llm_lock = asyncio.Lock()
        self._stt_sem = asyncio.Semaphore(MAX_CONCURRENT_STT)
        # Per-speaker follow-up tracking: only the speaker who addressed her
        # by name keeps the follow-up window open. Other speakers in the room
        # stay gated until they say her name themselves.
        self._engaged_with: dict[str, float] = {}  # speaker_id -> last reply mtime
        self._busy = False  # True while a turn is in flight (LLM or TTS)
        # Multi-tab dedup: if the same transcript text comes in from any speaker
        # within DEDUP_WINDOW_S, drop it. Catches the "Chef has two browser tabs
        # open, both publishing his mic" pattern that makes Lumina respond
        # twice to a single sentence.
        self._recent_transcripts: list[tuple[str, float]] = []
        # Broadcast-style follow-up: when she speaks (any reason — STT loop OR
        # data-channel /speak announcement), open a window for ANY speaker to
        # respond without saying her name. Refreshed on every utterance she
        # produces.
        self._broadcast_speak_t = 0.0
        # Barge-in: track the current speak task so the listener can cancel it
        # when the user starts talking mid-reply.
        self._current_speak: Optional[asyncio.Task] = None

    def interrupt(self) -> bool:
        """Cancel any in-flight speak task. Returns True if something was canceled."""
        if self._current_speak is not None and not self._current_speak.done():
            log.info("[lumina] interrupted")
            self._current_speak.cancel()
            return True
        return False

    async def set_state(self, state: str, detail: str = "") -> None:
        """Publish a state change via LiveKit participant metadata.

        Webui subscribes to participant.metadataChanged and surfaces a status
        pill (idle / listening / thinking / searching / speaking + detail).
        Browser sees it via the standard LiveKit JS event.
        """
        try:
            payload = json.dumps({"state": state, "detail": detail})
            await self.speaker.room.local_participant.set_metadata(payload)
            log.info("state → %s (%s)", state, detail[:60])
        except Exception as exc:
            log.warning("set_metadata failed: %s", exc)

    async def aclose(self) -> None:
        await self.client.aclose()

    def _is_addressed(self, speaker_id: str, text: str) -> bool:
        # Wake-word match: this speaker is now engaged with her.
        if _ADDRESS_RE.search(text):
            self._engaged_with[speaker_id] = time.monotonic()
            return True
        # Continuing a thread the SAME speaker recently opened — natural
        # follow-up turns from them roll forward without re-saying her name.
        last = self._engaged_with.get(speaker_id)
        if last is not None and time.monotonic() - last < FOLLOW_UP_WINDOW_S:
            return True
        # Broadcast follow-up window: when she just spoke (announcement or
        # reply), the next utterance from ANYONE counts as a reply to her.
        # Refreshes per-speaker on engagement.
        if time.monotonic() - self._broadcast_speak_t < FOLLOW_UP_WINDOW_S:
            self._engaged_with[speaker_id] = time.monotonic()
            return True
        return False

    async def handle_utterance(self, speaker_id: str, pcm16k: bytes) -> None:
        if self._stt_sem.locked():
            log.debug("STT busy — dropping utterance from %s", speaker_id)
            return
        async with self._stt_sem:
            try:
                text = await transcribe(self.client, pcm16k)
            except Exception as exc:
                log.warning("STT failed (%s): %r", type(exc).__name__, exc)
                return
        if not text or len(text) < 2:
            return

        # Multi-tab / multi-mic dedup: if a near-identical transcript came
        # through from any speaker in the last DEDUP_WINDOW_S, drop this one.
        # When Chef has two browser tabs open, both publish his mic, both
        # produce a transcript, and Lumina responds twice without this guard.
        now = time.monotonic()
        normalized = text.lower().strip().rstrip(".,!?")
        self._recent_transcripts = [
            (t, ts) for (t, ts) in self._recent_transcripts
            if now - ts < DEDUP_WINDOW_S
        ]
        if any(t == normalized for (t, _ts) in self._recent_transcripts):
            log.info("· [%s] %s  (dup of recent — dropped)", speaker_id, text[:80])
            return
        self._recent_transcripts.append((normalized, now))

        # Drop whisper repetition hallucinations: short token repeated >5 times
        # ("If If If If If if if if If If If If If" or "Bye. Bye. Bye." patterns).
        words = text.split()
        if len(words) >= 6:
            lowers = [w.lower().strip(".,!?\"'") for w in words]
            top_word = max(set(lowers), key=lowers.count)
            if lowers.count(top_word) >= len(words) * 0.6 and len(top_word) <= 4:
                log.info("· [%s] %s  (whisper repetition — dropped)", speaker_id, text[:80])
                return

        addressed = self._is_addressed(speaker_id, text)
        marker = "→" if addressed else "·"  # · = overheard, not engaged
        log.info("%s [%s] %s", marker, speaker_id, text)
        if not addressed:
            return

        await self.set_state("thinking", text[:60])

        # If she's already mid-turn, INTERRUPT the current turn rather than
        # dropping the new utterance. Earlier we'd drop new turns to avoid
        # stacking LLM+TTS roundtrips, but that meant follow-up questions
        # from Chef got swallowed during long replies — looked like she was
        # ignoring him. Now: cancel current turn, process the new one.
        if self._busy:
            log.info("interrupting in-flight turn for new utterance from %s", speaker_id)
            self.interrupt()
            # Wait briefly for the cancellation to propagate so we don't
            # double-stack a turn against a still-tearing-down one.
            await asyncio.sleep(0.05)

        self._busy = True
        try:
            async with self._llm_lock:
                try:
                    reply = await llm_reply(self.client, self.history, f"{speaker_id}: {text}",
                                            on_tool=self._on_tool_event)
                except Exception as exc:
                    log.warning("LLM failed (%s): %r", type(exc).__name__, exc)
                    return
            if not reply:
                return
            log.info("[lumina] %s", reply)
            await self.set_state("speaking", reply[:80])
            await self.say(reply)
            # Refresh the speaker's follow-up window as of the end of her reply.
            self._engaged_with[speaker_id] = time.monotonic()
        finally:
            self._busy = False
            await self.set_state("idle", "")

    async def _on_tool_event(self, name: str, args: dict) -> None:
        """Surface tool-call activity to the webui via metadata."""
        if name == "search_memory":
            q = (args.get("query") or "")[:60]
            await self.set_state("searching", f"memory: {q}")
        else:
            await self.set_state("thinking", f"tool: {name}")

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        """Break a reply into sentences for sequential TTS synthesis.

        Short fragments (<16 chars) get merged with neighbors — VoxCPM's
        synthesis quality degrades on tiny inputs (often produces 1-second
        truncated audio for ~150-char sentences when batched alongside a
        7-char fragment, even sequentially). Very long single sentences
        keep their own slot for synthesis pacing.
        """
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\"'])", text.strip())
        cleaned: list[str] = []
        for p in parts:
            p = p.strip()
            if not p:
                continue
            if cleaned and len(p) < 16:
                cleaned[-1] = (cleaned[-1].rstrip() + " " + p).strip()
            elif not cleaned and len(p) < 16:
                # Short first fragment — hold it, prepend to the next sentence.
                cleaned.append(p)  # placeholder
            else:
                if cleaned and len(cleaned[-1]) < 16:
                    cleaned[-1] = (cleaned[-1].rstrip() + " " + p).strip()
                else:
                    cleaned.append(p)
        return cleaned

    async def say(self, text: str) -> None:
        # Whenever she speaks — for any reason — open the broadcast window.
        self._broadcast_speak_t = time.monotonic()
        # Strip stage directions / markdown that TTS would read literally.
        text = re.sub(r"\([^)]{1,80}\)", "", text)
        text = re.sub(r"\*[^*]{1,80}\*", "", text)
        text = re.sub(r"\[[^\]]{1,80}\]", "", text)
        # Strip UUIDs and long hex/numeric IDs — VoxCPM mangles them into
        # gibberish/stutters, sounds like she's "repeating herself".
        text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "(ID)", text, flags=re.I)
        text = re.sub(r"\b[0-9a-f]{16,}\b", "(ID)", text, flags=re.I)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            log.info("[lumina] (only stage-direction emitted, nothing to speak)")
            return
        # Surface speaking state for the webui (handle_utterance also sets this,
        # but the data-channel /speak announcement path lands here directly).
        await self.set_state("speaking", text[:80])

        # Split into sentences and synthesize in parallel. Playback waits for each
        # in order so the listener hears them sequentially, but the LLM-to-first-
        # audio latency drops to "first sentence synth time" instead of "whole
        # reply synth time". 50-70% perceived-latency cut on multi-sentence
        # replies; identical to single-shot for short ones.
        sentences = self._split_sentences(text)
        if len(sentences) <= 1:
            await self._speak_one(text)
            return

        log.info("speaking %d sentences (pipelined, per-sentence)", len(sentences))
        # Pipeline depth 2: while sentence N plays, N+1 synthesizes. Audio
        # starts ~1s after LLM finishes (synth time of sentence 1) instead of
        # waiting for ALL sentences to render. The earlier concat-stream
        # variant felt slow because nothing played until full reply was ready.
        synth_tasks: list = [None] * len(sentences)
        synth_tasks[0] = asyncio.create_task(synthesize(self.client, sentences[0]))
        try:
            for i, s in enumerate(sentences):
                if i + 1 < len(sentences) and synth_tasks[i + 1] is None:
                    synth_tasks[i + 1] = asyncio.create_task(
                        synthesize(self.client, sentences[i + 1])
                    )
                try:
                    pcm, sr, _, _ = await synth_tasks[i]
                except Exception as exc:
                    log.warning("TTS sentence %d/%d failed (%s): %r",
                                i + 1, len(sentences), type(exc).__name__, exc)
                    continue
                self._current_speak = asyncio.create_task(self.speaker.say(pcm, sr))
                try:
                    await self._current_speak
                except asyncio.CancelledError:
                    log.info("speak cancelled at sentence %d/%d", i + 1, len(sentences))
                    for t in synth_tasks[i + 1:]:
                        if t is not None and not t.done():
                            t.cancel()
                    return
                finally:
                    self._current_speak = None
        finally:
            await self.set_state("idle", "")

    async def _speak_one(self, text: str) -> None:
        """Single-sentence path — kept separate so the lipsync code path stays
        intact for the case where MuseTalk is enabled (it won't co-exist cleanly
        with sentence-parallel synthesis without further work)."""
        try:
            pcm, sr, _, wav_bytes = await synthesize(self.client, text)
        except Exception as exc:
            log.warning("TTS failed (%s): %r", type(exc).__name__, exc)
            return

        # Lip-sync still supported on the single-sentence path.
        frames: list[bytes] = []
        if av is not None and self.speaker.video_source is not None:
            t0 = time.monotonic()
            frames = await lipsync_frames(self.client, wav_bytes)
            if frames:
                log.info("lipsync %d frames in %.1fs", len(frames), time.monotonic() - t0)

        async def _play_audio() -> None:
            await self.speaker.say(pcm, sr)

        async def _play_video() -> None:
            if not frames:
                return
            self.speaker._lipsync_active = True
            try:
                interval = 1.0 / MUSETALK_FPS
                next_t = time.monotonic()
                for f in frames:
                    self.speaker.push_video_frame_rgba(f)
                    next_t += interval
                    delay = max(0.0, next_t - time.monotonic())
                    await asyncio.sleep(delay)
            finally:
                self.speaker._lipsync_active = False

        await asyncio.gather(_play_audio(), _play_video())


# ─── Token mint ───────────────────────────────────────────────────────────────
def mint_token(identity: str, name: str, room: str) -> dict:
    body = json.dumps({"identity": identity, "name": name, "room": room}).encode()
    req = urllib.request.Request(
        f"{WEBUI_URL}/livekit/token",
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


# ─── Main ─────────────────────────────────────────────────────────────────────
async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--room", default=DEFAULT_ROOM)
    parser.add_argument(
        "--greet",
        default="Hi Chef, I'm here. I can hear you now — talk to me.",
        help="opening line; pass empty string to stay silent on join",
    )
    parser.add_argument("--no-listen", action="store_true", help="disable STT/LLM, only respond to data channel speak commands")
    args = parser.parse_args()

    log.info("minting token for %s @ %s", IDENTITY, args.room)
    t = mint_token(IDENTITY, DISPLAY_NAME, args.room)
    log.info("connecting %s", t["url"])

    room = rtc.Room()
    speaker = Speaker(room)
    convo = Conversation(speaker)

    listen_tasks: list[asyncio.Task] = []
    listening_to: set[str] = set()  # track sids we're already consuming

    def _start_listening(track: rtc.Track, p: rtc.RemoteParticipant) -> None:
        if track.kind != rtc.TrackKind.KIND_AUDIO or args.no_listen:
            return
        if track.sid in listening_to:
            return  # already have a listener — track_subscribed event re-fires on reconnect
        listening_to.add(track.sid)
        log.info("→ subscribing to audio from %s (track %s)", p.identity, track.sid)
        listen_tasks.append(
            asyncio.create_task(listen_to_participant(p, track, convo.handle_utterance, speaker, convo.interrupt))
        )

    @room.on("track_subscribed")
    def _on_track(track: rtc.Track, _pub: rtc.RemoteTrackPublication, p: rtc.RemoteParticipant) -> None:
        _start_listening(track, p)

    @room.on("data_received")
    def _on_data(packet: rtc.DataPacket) -> None:
        try:
            payload = json.loads(bytes(packet.data).decode())
        except Exception:
            return
        if payload.get("action") == "speak" and payload.get("text"):
            log.info("data-channel speak from %s: %r",
                     packet.participant.identity if packet.participant else "?",
                     payload["text"])
            asyncio.create_task(convo.say(payload["text"]))

    @room.on("participant_connected")
    def _on_pc(p: rtc.RemoteParticipant) -> None:
        log.info("+ %s", p.identity)

    @room.on("participant_disconnected")
    def _on_pd(p: rtc.RemoteParticipant) -> None:
        log.info("- %s", p.identity)

    await room.connect(t["url"], t["token"])
    log.info("connected: room=%s sid=%s", room.name, await room.sid)
    log.info("existing peers: %s",
             [p.identity for p in room.remote_participants.values()] or "(none)")

    await speaker.publish()
    log.info("audio track published")

    # Subscribe to existing participants' tracks (track_subscribed will fire
    # for already-subscribed tracks too, but the dedup set above guards us).
    for p in room.remote_participants.values():
        for pub in p.track_publications.values():
            if pub.subscribed and pub.track:
                _start_listening(pub.track, p)

    if args.greet:
        await asyncio.sleep(0.6)  # let subscriptions settle
        await convo.say(args.greet)

    # Run until signaled
    stop = asyncio.Event()
    def _shutdown(*_: object) -> None:
        log.info("shutdown signal — leaving room")
        stop.set()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    log.info("ready — listening for speech %s", "(disabled)" if args.no_listen else "and data-channel commands")
    await stop.wait()

    for task in listen_tasks:
        task.cancel()
    await convo.aclose()
    await room.disconnect()
    log.info("disconnected")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
