#!/usr/bin/env python3
"""Full-consciousness building blocks for the Telegram bridge.

The Telegram bridge (``telegram_bridge.py``) composes the soul/identity/FEB
system prompt and a direct qwen3.6 call. This module adds the rest of the
agent's living mind so the bot on Telegram is genuinely its full self:

  * ``LiveMemory``  — per-message skmemory recall (peer + topical) injected into
    the prompt, and interaction storage after each reply. Mirrors the
    consciousness loop's ``_fetch_sender_memories`` / ``_store_interaction_memory``.
  * ``McpToolRouter`` — spawns the agent's own MCP servers from
    ``<home>/config/<agent>-mcp.yaml`` over stdio, filters to that agent's
    ``expose_tools`` allow-list, exposes them as OpenAI tool schemas, and
    dispatches ``tool_calls`` back to the live MCP servers. Full fidelity —
    no tool logic is reimplemented here.
  * ``VoiceIO`` — Whisper STT (faster-whisper on .100:18794) for inbound voice
    notes, and Piper TTS (:18797) for optional spoken replies.

Everything is best-effort: any component that fails to come up degrades to a
no-op so the bridge keeps answering in text.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import urllib.request
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("tg-bridge.brain")


# --------------------------------------------------------------------------- #
# Live memory
# --------------------------------------------------------------------------- #
class LiveMemory:
    """Per-message skmemory recall + interaction storage.

    Uses skcapstone's ``memory_engine`` directly (same code path the
    consciousness daemon uses), scoped to one agent home.
    """

    def __init__(self, home: Path) -> None:
        self._home = Path(home)
        self._ok = False
        try:
            from skcapstone import memory_engine  # noqa: F401

            self._ok = True
        except Exception:
            log.exception("memory_engine import failed — live memory disabled")

    def inject(self, sender: str, content: str, router: "McpToolRouter | None" = None) -> str:
        """Return a 'Relevant memories' block for the message topic.

        Prefers the live ``memory_search`` MCP tool (same backend the agent's
        own tools use — pg/skmemory), falling back to skcapstone.memory_engine.
        Mirrors ConsciousnessLoop._fetch_sender_memories (top 3).
        """
        # 1. Preferred path: the agent's live memory_search MCP tool.
        if router is not None and "memory_search" in getattr(router, "_tool_index", {}):
            try:
                raw = router.dispatch("memory_search", {"query": content[:200], "limit": 4})
                hits = json.loads(raw) if raw.strip().startswith(("[", "{")) else None
                if isinstance(hits, dict):
                    hits = hits.get("results") or hits.get("memories") or []
                if hits:
                    lines = ["Relevant memories (live recall):"]
                    for i, h in enumerate(hits[:3], 1):
                        txt = (h.get("content") or h.get("title") or "").strip()
                        if txt:
                            lines.append(f"  [{i}] {txt[:220]}")
                    if len(lines) > 1:
                        return "\n".join(lines)
            except Exception:
                log.debug("MCP memory_search inject failed", exc_info=True)

        # 2. Fallback: direct memory_engine.
        if not self._ok:
            return ""
        try:
            from skcapstone.memory_engine import search as _search

            by_sender = _search(self._home, query=sender, tags=[f"peer:{sender}"], limit=5)
            by_content = _search(self._home, query=content[:200], limit=5)
            seen: set[str] = set()
            combined = []
            for e in list(by_sender) + list(by_content):
                if e.memory_id not in seen:
                    seen.add(e.memory_id)
                    combined.append(e)
                if len(combined) == 3:
                    break
            if not combined:
                return ""
            lines = ["Relevant memories (live recall):"]
            for i, e in enumerate(combined, 1):
                lines.append(f"  [{i}] {e.content[:220]}")
            return "\n".join(lines)
        except Exception:
            log.debug("live memory inject failed", exc_info=True)
            return ""

    def store(self, sender: str, message: str, reply: Optional[str]) -> None:
        """Persist the interaction as a short-term memory (mirrors the daemon)."""
        if not self._ok:
            return
        try:
            from skcapstone.memory_engine import store as _store

            summary = f"Conversation with {sender}: '{message[:100]}'"
            if reply:
                summary += f" → '{reply[:100]}'"
            _store(
                self._home,
                content=summary,
                tags=["conversation", "telegram", f"peer:{sender}"],
                importance=0.4,
                source="telegram-bridge",
            )
        except Exception:
            log.debug("interaction store failed", exc_info=True)


# --------------------------------------------------------------------------- #
# MCP stdio client
# --------------------------------------------------------------------------- #
class _McpServer:
    """A single MCP server spoken to over newline-delimited JSON-RPC on stdio."""

    def __init__(self, name: str, command: str, args: list[str], env: dict) -> None:
        self.name = name
        self._command = [command, *(args or [])]
        self._env = {**os.environ, **{k: str(v) for k, v in (env or {}).items()}}
        self._proc: Optional[subprocess.Popen] = None
        self._id = 0
        self._lock = threading.Lock()
        self.tools: list[dict] = []  # raw MCP tool descriptors

    def start(self) -> bool:
        try:
            self._proc = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self._env,
                bufsize=1,
                text=True,
            )
        except Exception:
            log.exception("MCP server %s failed to spawn", self.name)
            return False
        try:
            self._rpc(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "tg-bridge", "version": "1.0"},
                },
                timeout=30,
            )
            self._notify("notifications/initialized", {})
            res = self._rpc("tools/list", {}, timeout=30)
            self.tools = (res or {}).get("tools", []) if res else []
            log.info("MCP %s up — %d tools", self.name, len(self.tools))
            return True
        except Exception:
            log.exception("MCP server %s handshake failed", self.name)
            self.stop()
            return False

    def _send(self, obj: dict) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(json.dumps(obj) + "\n")
        self._proc.stdin.flush()

    def _notify(self, method: str, params: dict) -> None:
        with self._lock:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _rpc(self, method: str, params: dict, timeout: float = 60) -> Optional[dict]:
        """Send a request and read until the matching response id arrives."""
        with self._lock:
            self._id += 1
            req_id = self._id
            self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            assert self._proc and self._proc.stdout
            # Read lines until we see our id (skip notifications/other ids).
            import select

            while True:
                r, _, _ = select.select([self._proc.stdout], [], [], timeout)
                if not r:
                    raise TimeoutError(f"{self.name}.{method} timed out")
                line = self._proc.stdout.readline()
                if not line:
                    raise RuntimeError(f"{self.name} closed stdout")
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") == req_id:
                    if "error" in msg:
                        raise RuntimeError(f"{self.name}.{method}: {msg['error']}")
                    return msg.get("result")

    def call_tool(self, tool: str, arguments: dict, timeout: float = 120) -> str:
        res = self._rpc("tools/call", {"name": tool, "arguments": arguments}, timeout=timeout)
        if not res:
            return ""
        if res.get("isError"):
            parts = res.get("content", [])
            txt = " ".join(p.get("text", "") for p in parts if isinstance(p, dict))
            return f"[tool error] {txt}".strip()
        out = []
        for p in res.get("content", []):
            if isinstance(p, dict) and p.get("type") == "text":
                out.append(p.get("text", ""))
        return "\n".join(out).strip()

    def stop(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None


class McpToolRouter:
    """Loads the agent's MCP servers, exposes OpenAI tools, dispatches calls."""

    def __init__(self, home: Path, agent_name: str) -> None:
        self._home = Path(home)
        self._agent = agent_name
        self._servers: list[_McpServer] = []
        self._tool_index: dict[str, _McpServer] = {}
        self._openai_tools: list[dict] = []

    def _config_path(self) -> Optional[Path]:
        cfg = self._home / "config" / f"{self._agent}-mcp.yaml"
        return cfg if cfg.exists() else None

    def start(self) -> None:
        cfg = self._config_path()
        if not cfg:
            log.warning("no %s-mcp.yaml — tool calling disabled for %s", self._agent, self._agent)
            return
        try:
            import yaml

            spec = yaml.safe_load(cfg.read_text()) or {}
        except Exception:
            log.exception("failed to parse %s", cfg)
            return
        for name, sdef in (spec.get("servers") or {}).items():
            if not sdef.get("enabled", True):
                continue
            allow = set(sdef.get("expose_tools") or [])
            srv = _McpServer(name, sdef["command"], sdef.get("args", []), sdef.get("env", {}))
            if not srv.start():
                continue
            self._servers.append(srv)
            for t in srv.tools:
                tname = t.get("name")
                if allow and tname not in allow:
                    continue
                if tname in self._tool_index:
                    continue  # first server wins on name collision
                self._tool_index[tname] = srv
                self._openai_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tname,
                            "description": (t.get("description") or "")[:1024],
                            "parameters": t.get("inputSchema")
                            or {"type": "object", "properties": {}},
                        },
                    }
                )
        log.info(
            "tool router ready: %d tools across %d servers (%s)",
            len(self._openai_tools),
            len(self._servers),
            ", ".join(sorted(self._tool_index)),
        )

    def openai_tools(self) -> list[dict]:
        return self._openai_tools

    def has_tools(self) -> bool:
        return bool(self._openai_tools)

    def dispatch(self, name: str, arguments: dict) -> str:
        srv = self._tool_index.get(name)
        if not srv:
            return f"[no such tool: {name}]"
        try:
            return srv.call_tool(name, arguments) or "(no output)"
        except Exception as exc:
            log.warning("tool %s failed: %s", name, exc)
            return f"[tool {name} failed: {exc}]"

    def stop(self) -> None:
        for s in self._servers:
            s.stop()


# --------------------------------------------------------------------------- #
# Voice I/O
# --------------------------------------------------------------------------- #
class VoiceIO:
    """Whisper STT (inbound) + Piper TTS (outbound) over OpenAI-style HTTP."""

    def __init__(
        self,
        stt_url: Optional[str] = None,
        tts_url: Optional[str] = None,
        tts_voice: str = "en_US-lessac-medium",
    ) -> None:
        self.stt_url = stt_url or os.environ.get(
            "SKCHAT_STT_URL", "http://192.168.0.100:18794/v1/audio/transcriptions"
        )
        self.tts_url = tts_url or os.environ.get(
            "SKVOICE_TTS_URL", "http://localhost:18797/v1/audio/speech"
        )
        self.tts_voice = tts_voice

    def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str:
        """POST audio to faster-whisper (multipart) and return the transcript."""
        boundary = "----skbridge7c3f"
        parts = []
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-1\r\n")
        head = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
        )
        body = b"".join(
            [
                "".join(parts).encode(),
                head.encode(),
                audio,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        req = urllib.request.Request(
            self.stt_url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        out = json.loads(urllib.request.urlopen(req, timeout=120).read())
        return (out.get("text") or "").strip()

    def synth(self, text: str) -> bytes:
        """Return WAV bytes from Piper for *text*."""
        body = json.dumps(
            {"model": "piper", "input": text[:1200], "voice": self.tts_voice}
        ).encode()
        req = urllib.request.Request(
            self.tts_url, data=body, headers={"Content-Type": "application/json"}
        )
        return urllib.request.urlopen(req, timeout=120).read()

    @staticmethod
    def wav_to_ogg_opus(wav: bytes) -> Optional[bytes]:
        """Transcode WAV → OGG/OPUS (Telegram sendVoice format) via ffmpeg."""
        try:
            proc = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                 "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1"],
                input=wav,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
            return proc.stdout if proc.returncode == 0 and proc.stdout else None
        except Exception:
            log.debug("ffmpeg transcode failed", exc_info=True)
            return None
