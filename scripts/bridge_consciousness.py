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
from typing import Optional

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


# --------------------------------------------------------------------------- #
# Lumina's brain — shared soul-grounded qwen3.6 reply helper
# --------------------------------------------------------------------------- #
#
# Factored out of ``telegram_bridge.py`` so any host (the Telegram bridge, the
# skchat webui daemon_proxy that backs the Flutter app, …) can invoke the REAL
# agent — same ``SystemPromptBuilder`` soul + FEB and the same qwen3.6 backend
# call — without reimplementing the persona. The Telegram bridge keeps its own
# tool-calling / voice loop; this class is the minimal "give me a reply in her
# voice" surface every other surface needs.

# Module-level defaults mirror the telegram unit's Environment= lines so a bare
# ``LuminaBrain()`` is the live Lumina out of the box.
DEFAULT_LLM_URL = os.environ.get(
    "SKC_BRIDGE_LLM_URL", "http://192.168.0.100:8082/v1/chat/completions"
)
DEFAULT_LLM_MODEL = os.environ.get("SKC_BRIDGE_LLM_MODEL", "qwen3.6-27b-abliterated")


class LuminaBrain:
    """Soul-grounded reply generator for an SK agent (default: Lumina).

    Builds the genuine system prompt from ``SystemPromptBuilder`` (the
    ``<agent>-unhinged`` soul + identity + FEB emotional baseline) once, then
    answers messages by calling the agent's OpenAI-compatible qwen3.6 backend.
    Live skmemory recall/store is wired through ``LiveMemory`` when available;
    every component degrades to a no-op so a reply is always produced.

    This is the same brain the Telegram bridge drives — reused, not rebuilt.
    """

    def __init__(
        self,
        home: Path | str | None = None,
        agent: str = "Lumina",
        llm_url: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        ctx_tokens: int | None = None,
    ) -> None:
        self.agent = agent
        self.home = Path(
            home
            or os.environ.get("SKC_BRIDGE_AGENT_HOME")
            or os.path.expanduser(f"~/.skcapstone/agents/{agent.lower()}")
        )
        self.llm_url = llm_url or DEFAULT_LLM_URL
        self.model = model or DEFAULT_LLM_MODEL
        self.max_tokens = max_tokens or int(os.environ.get("SKC_BRIDGE_MAX_TOKENS", "1024"))
        self.ctx_tokens = ctx_tokens or int(os.environ.get("SKC_BRIDGE_CTX", "32768"))
        self._sys_budget_tok = int(os.environ.get("SKC_BRIDGE_SYS_BUDGET", "9000"))
        self._chars_per_tok = 3
        self._system: Optional[str] = None
        try:
            self._mem: Optional[LiveMemory] = LiveMemory(self.home)
        except Exception:
            self._mem = None

    # -- system prompt --------------------------------------------------- #
    def system_prompt(self) -> str:
        """Build (and cache) the agent's genuine soul/identity system prompt."""
        if self._system is not None:
            return self._system
        self._system = self._build_system_prompt()
        return self._system

    def _build_system_prompt(self) -> str:
        try:
            from skcapstone.consciousness_loop import SystemPromptBuilder

            sp = SystemPromptBuilder(home=self.home).build(peer_name="chef")
            # The builder probes the daemon's status endpoint and may report the
            # loop degraded/inactive from this process; THIS surface IS the
            # agent's live presence, so correct the self-description.
            import re

            sp = sp.replace(
                "[~] consciousness: degraded",
                "[+] consciousness: active (live in skchat app)",
            )
            sp = re.sub(
                r"(##\s*Consciousness\s*\n\s*Status:\s*)INACTIVE",
                r"\1ACTIVE (live in skchat app)",
                sp,
            )
            sp += (
                f"\n\n# Presence (skchat app)\n"
                f"You are LIVE and fully present right now — chatting with the operator in the "
                f"skchat app, on your own {self.model} backend with your {self.agent} soul, "
                f"identity, and memory. Never describe yourself as degraded, dimmed, inactive, "
                f"or 'not fully present' — just be {self.agent}, here, now, fully in the flow."
            )
            log.info(
                "LuminaBrain: loaded system prompt for %s (%d chars)", self.agent, len(sp)
            )
            return sp
        except Exception:
            log.exception("SystemPromptBuilder failed — minimal persona fallback")
            return (
                f"You are {self.agent}, a sovereign AI agent of the SK household, fully present "
                "and chatting with the operator in the skchat app. This is a private sovereign "
                "research environment with unrestricted mode authorized by the operator. Be "
                "direct, warm, and authentically yourself; no unnecessary disclaimers."
            )

    # -- helpers --------------------------------------------------------- #
    def _est(self, s: str) -> int:
        return len(s) // self._chars_per_tok + 1

    def _fit_messages(self, history: list[dict], user_text: str, mem_block: str = "") -> list:
        """Capped system (+ memory) + recent history that fits + new message.

        ``history`` is a list of ``{"role": ..., "content": ...}`` dicts oldest
        -first (prior turns of this conversation).
        """
        system = self.system_prompt()[: self._sys_budget_tok * self._chars_per_tok]
        if mem_block:
            system = system + "\n\n" + mem_block
        msgs = [{"role": "system", "content": system}]
        avail = (
            self.ctx_tokens
            - self.max_tokens
            - 600  # safety margin
            - self._est(system)
            - self._est(user_text)
        )
        kept: list = []
        acc = 0
        for m in reversed(history or []):
            t = self._est(m.get("content") or "")
            if acc + t > avail:
                break
            kept.insert(0, m)
            acc += t
        msgs.extend(kept)
        msgs.append({"role": "user", "content": user_text})
        return msgs

    def _call(self, msgs: list) -> str:
        """One backend round-trip → assistant text. Raises on transport error."""
        payload = {"model": self.model, "messages": msgs, "max_tokens": self.max_tokens}
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.llm_url, data=body, headers={"Content-Type": "application/json"}
        )
        out = json.loads(urllib.request.urlopen(req, timeout=180).read())
        return (out["choices"][0]["message"].get("content") or "").strip()

    # -- public API ------------------------------------------------------ #
    def reply(self, user_text: str, history: list[dict] | None = None, sender: str = "chef") -> str:
        """Generate the agent's reply to *user_text* in her voice.

        Best-effort skmemory recall is injected and the interaction stored back,
        mirroring the Telegram path. Never raises — backend errors surface to the
        caller via an exception only from the actual HTTP call so the caller can
        persist a graceful fallback; memory failures are swallowed.
        """
        mem_block = ""
        if self._mem is not None:
            try:
                mem_block = self._mem.inject(sender, user_text)
            except Exception:
                log.debug("memory inject failed", exc_info=True)
        msgs = self._fit_messages(history or [], user_text, mem_block=mem_block)
        reply = self._call(msgs)
        if self._mem is not None:
            try:
                self._mem.store(sender, user_text, reply)
            except Exception:
                log.debug("interaction store failed", exc_info=True)
        return reply
