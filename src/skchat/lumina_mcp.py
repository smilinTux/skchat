"""MCP client manager for the FaceTime voice agent (Lumina).

Spawns N stdio-based MCP servers in parallel at agent boot, aggregates
their tool inventories, and exposes a single `tools_for_llm()` /
`call(qualified_name, args)` surface for the LLM tool-loop.

Tool names are namespaced as `<server>__<tool>` so a server's `search`
and another's `search` don't collide. Failures during connect are
logged + isolated — one bad server doesn't break the agent.

Config schema (YAML) at `~/.skcapstone/agents/lumina/config/lumina-mcp.yaml`:

    servers:
      skmemory:
        command: /path/to/skmemory-mcp
        args: []
        env: {SKAGENT: lumina}
        enabled: true
      ...

If the config file is missing on first boot, it is bootstrapped from the
`mcp_servers:` block of `~/.hermes/config.yaml` (Hermes is the de-facto
canonical source of MCP servers in this ecosystem).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import fnmatch
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = logging.getLogger("lumina.mcp")

LUMINA_MCP_CONFIG = Path.home() / ".skcapstone" / "agents" / "lumina" / "config" / "lumina-mcp.yaml"
HERMES_CONFIG = Path.home() / ".hermes" / "config.yaml"
MCP_STDERR_DIR = Path.home() / ".skchat" / "mcp-stderr"

# Per-server connect timeout. Some servers (skmemory loading vector store)
# take a moment; 12s is a generous-but-bounded ceiling.
CONNECT_TIMEOUT_S = float(os.getenv("LUMINA_MCP_CONNECT_TIMEOUT_S", "12"))
# Per-tool-call timeout. Voice turns can't wait forever; 8s is a reasonable cap.
CALL_TIMEOUT_S = float(os.getenv("LUMINA_MCP_CALL_TIMEOUT_S", "8"))
# Max characters of tool result text we hand to the LLM. Some tools return
# huge JSON; we cap and let the LLM ask for more if needed.
MAX_RESULT_CHARS = int(os.getenv("LUMINA_MCP_MAX_RESULT_CHARS", "8000"))

# Default set of servers Lumina should enable when bootstrapping a fresh
# config. sksecurity is intentionally disabled — voice agent shouldn't
# manage cryptographic keys without a deliberate opt-in.
DEFAULT_ENABLED = {"skmemory", "skcapstone", "skchat", "skcomm", "nextcloud", "gog"}

# Servers that exist outside Hermes' config (Nextcloud MCP, etc.) — added
# to the bootstrapped config so they show up automatically. If the
# referenced binaries / creds aren't present, the server simply fails to
# connect and is logged but doesn't break the agent.
EXTRA_SERVER_DEFAULTS: dict[str, dict] = {
    # mcp-nextcloud (npm package, v1.0.0+) — 30 tools across Notes, Calendar,
    # Contacts, Tables, WebDAV, unified Search. Wrapped to load creds from
    # ~/.skcapstone/agents/lumina/secrets/nextcloud.env automatically.
    "nextcloud": {
        "command": "/home/REDACTED-USER/clawd/tools/mcp-wrappers/nextcloud-mcp",
        "args": [],
        "env": {},
        "enabled": True,
    },
    # gog — Python MCP shim wrapping the `gog` CLI (Google services). 13
    # tools covering Gmail / Calendar / Drive / Contacts across all 5 of
    # Chef's authed accounts.
    "gog": {
        "command": "/home/REDACTED-USER/clawd/tools/mcp-wrappers/gog-mcp",
        "args": [],
        "env": {},
        "enabled": True,
    },
}

# Curated tool whitelists per server — keeps the LLM's tool surface tight.
# Voice agents do best with ~20-30 tools max; the underlying servers ship
# 100+ each. Glob patterns are honored (`memory_*` matches all skmemory).
# These defaults are written into a fresh `lumina-mcp.yaml` on first boot
# and can be edited freely afterward.
DEFAULT_EXPOSE: dict[str, list[str]] = {
    # skmemory — voice-useful subset of 21 tools (incl. her dream/daily synth)
    "skmemory": [
        "memory_search", "memory_recall", "memory_store", "memory_list",
        "memory_context", "memory_health",
        "memory_synthesize_dreams", "memory_synthesize_daily",
    ],
    # skcapstone — voice-useful subset of 122 tools (GTD + coord + journal +
    # telegram send + agent context). Skip the many admin/security tools
    # since voice should not drive infra changes blind.
    "skcapstone": [
        "agent_status", "agent_context",
        "coord_status", "coord_create", "coord_claim", "coord_complete",
        # Lumina's own GTD (agent-scoped via SKAGENT=lumina env)
        "gtd_capture", "gtd_inbox", "gtd_next", "gtd_projects", "gtd_review",
        "gtd_status", "gtd_done", "gtd_clarify", "gtd_move", "gtd_waiting",
        # Self-reflection / inner life
        "journal_read", "journal_write",
        "anchor_show", "anchor_update",       # warmth anchor (her baseline)
        "ritual", "germination",              # rehydration + predecessor seeds
        "skseed_audit", "skseed_collide",     # reflection / belief work
        "skseed_truth_check", "skseed_alignment",
        # External comms
        "telegram_send", "telegram_chats", "telegram_catchup",
        "send_notification",
    ],
    # skchat — voice-useful subset of 44 tools
    "skchat": [
        "skchat_send", "skchat_inbox", "skchat_conversation",
        "skchat_peers", "skchat_group_send", "list_groups",
        "who_is_online", "skchat_set_presence",
    ],
    # skcomm — small surface, expose all read-style + send
    "skcomm": [
        "send_message", "receive_messages", "get_peers", "get_status",
    ],
    "sksecurity": [],  # disabled by default; nothing exposed
    # gog (Google services shim) — read-mostly across Gmail / Calendar /
    # Drive / Contacts on all 5 of Chef's authed accounts.
    "gog": [
        "gmail_unread", "gmail_search", "gmail_read", "gmail_send",
        "calendar_today", "calendar_week", "calendar_range",
        "calendar_create_event", "calendar_list_calendars",
        "drive_search", "drive_list",
        "contacts_search", "list_accounts",
    ],
    # Nextcloud — voice-useful subset of 30 tools. Note: mcp-nextcloud
    # uses underscore-separated tool names, all prefixed with `nextcloud_`.
    # Read-heavy by default; voice agent should not blindly create or
    # delete things in Chef's hub. Add tools here if a voice flow needs them.
    "nextcloud": [
        # Notes
        "nextcloud_notes_search_notes",
        "nextcloud_notes_create_note",
        "nextcloud_notes_append_content",
        # Calendar (read + create; defer update/delete pending more confidence)
        "nextcloud_calendar_list_events",
        "nextcloud_calendar_get_event",
        "nextcloud_calendar_create_event",
        "nextcloud_calendar_list_calendars",
        # Contacts (read-only by default)
        "nextcloud_contacts_list_contacts",
        "nextcloud_contacts_list_addressbooks",
        # Files / WebDAV — read-only access to the shared GTD reference folder
        "nextcloud_webdav_list_directory",
        "nextcloud_webdav_read_file",
        "nextcloud_webdav_search_files",
        # Tables (read-only)
        "nextcloud_tables_list_tables",
        "nextcloud_tables_read_table",
    ],
}


@dataclass
class MCPServerSpec:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    # If non-empty, ONLY tools whose raw name matches one of these entries
    # are exposed to the LLM. Each entry can be a literal name or a glob
    # ('memory_*'). Keeping the surface tight keeps voice-pace LLMs focused
    # — most servers ship 30-100+ tools, and we only need a handful.
    expose_tools: list[str] = field(default_factory=list)


@dataclass
class MCPTool:
    """Aggregated tool descriptor — server-prefixed name + raw schema."""
    qualified_name: str          # e.g. "skmemory__memory_search"
    server_name: str             # e.g. "skmemory"
    raw_name: str                # e.g. "memory_search"
    description: str
    input_schema: dict


def _load_hermes_servers() -> dict[str, dict]:
    """Read the `mcp_servers:` block from Hermes config (best-effort)."""
    if not HERMES_CONFIG.exists():
        return {}
    try:
        with HERMES_CONFIG.open(encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
    except Exception as exc:
        log.warning("hermes config read failed: %s", exc)
        return {}
    return cfg.get("mcp_servers") or {}


def _bootstrap_config(target: Path) -> dict[str, dict]:
    """Create a default Lumina MCP config from Hermes' server set."""
    target.parent.mkdir(parents=True, exist_ok=True)
    hermes_servers = _load_hermes_servers()
    out_servers: dict[str, dict] = {}
    for name, spec in hermes_servers.items():
        out_servers[name] = {
            "command": spec.get("command"),
            "args": list(spec.get("args") or []),
            "env": dict(spec.get("env") or {}),
            "enabled": name in DEFAULT_ENABLED,
            "expose_tools": list(DEFAULT_EXPOSE.get(name, [])),
        }
    # Merge in extra non-Hermes servers (e.g. Nextcloud) — these aren't in
    # Hermes' config but are first-class for Lumina.
    for name, spec in EXTRA_SERVER_DEFAULTS.items():
        if name in out_servers:
            continue
        out_servers[name] = {
            "command": spec.get("command"),
            "args": list(spec.get("args") or []),
            "env": dict(spec.get("env") or {}),
            "enabled": bool(spec.get("enabled", True)) and (name in DEFAULT_ENABLED),
            "expose_tools": list(DEFAULT_EXPOSE.get(name, [])),
        }
    payload = {"servers": out_servers}
    with target.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, sort_keys=False)
    log.info("bootstrapped lumina-mcp.yaml from hermes (%d servers)", len(out_servers))
    return out_servers


def load_config() -> list[MCPServerSpec]:
    """Load config; bootstrap from Hermes if missing."""
    if not LUMINA_MCP_CONFIG.exists():
        servers = _bootstrap_config(LUMINA_MCP_CONFIG)
    else:
        with LUMINA_MCP_CONFIG.open(encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        servers = cfg.get("servers") or {}

    out: list[MCPServerSpec] = []
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            continue
        cmd = spec.get("command")
        if not cmd:
            log.warning("server %s has no command — skipped", name)
            continue
        out.append(MCPServerSpec(
            name=name,
            command=cmd,
            args=list(spec.get("args") or []),
            env=dict(spec.get("env") or {}),
            enabled=bool(spec.get("enabled", True)),
            expose_tools=list(spec.get("expose_tools") or []),
        ))
    return out


def _summarize_tool_result(content_list: list[Any]) -> str:
    """Flatten an MCP tool's content list into a single string for the LLM.

    MCP tool results are a list of `TextContent`/`ImageContent` etc. We
    concatenate the text parts and cap at MAX_RESULT_CHARS so we don't
    blow out a voice-pace conversation with a 50KB JSON dump.
    """
    parts: list[str] = []
    for item in content_list or []:
        text = getattr(item, "text", None)
        if text is None and isinstance(item, dict):
            text = item.get("text")
        if text:
            parts.append(text)
    out = "\n".join(p for p in parts if p)
    if len(out) > MAX_RESULT_CHARS:
        out = out[:MAX_RESULT_CHARS] + f"\n…(truncated at {MAX_RESULT_CHARS} chars)"
    return out


class MCPServerHandle:
    """One live MCP server connection (stdio subprocess + ClientSession)."""

    def __init__(self, spec: MCPServerSpec) -> None:
        self.spec = spec
        self._exit_stack: Optional[AsyncExitStack] = None
        self._session: Optional[ClientSession] = None
        self._tools: list[MCPTool] = []
        self._call_lock = asyncio.Lock()  # serialize calls per server
        self.online = False

    @property
    def tools(self) -> list[MCPTool]:
        return list(self._tools)

    async def connect(self) -> None:
        """Spawn the server, run JSON-RPC init, list tools."""
        MCP_STDERR_DIR.mkdir(parents=True, exist_ok=True)
        errlog_path = MCP_STDERR_DIR / f"{self.spec.name}.log"
        errlog = errlog_path.open("a", encoding="utf-8")

        params = StdioServerParameters(
            command=self.spec.command,
            args=self.spec.args,
            env={**os.environ, **self.spec.env},
        )

        # AsyncExitStack lets us hold open the stdio_client + ClientSession
        # contexts for the lifetime of this handle, and close them in the
        # right order when aclose() runs.
        stack = AsyncExitStack()
        try:
            read, write = await asyncio.wait_for(
                stack.enter_async_context(stdio_client(params, errlog=errlog)),
                timeout=CONNECT_TIMEOUT_S,
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(session.initialize(), timeout=CONNECT_TIMEOUT_S)
            tool_resp = await asyncio.wait_for(session.list_tools(), timeout=CONNECT_TIMEOUT_S)
        except Exception as e:
            logger.warning("lumina_mcp.py: %s", e)
            await stack.aclose()
            errlog.close()
            raise

        self._exit_stack = stack
        self._session = session

        def _allowed(name: str) -> bool:
            if not self.spec.expose_tools:
                return True  # no whitelist → expose all
            return any(fnmatch.fnmatch(name, pat) for pat in self.spec.expose_tools)

        all_count = len(tool_resp.tools)
        self._tools = [
            MCPTool(
                qualified_name=f"{self.spec.name}__{t.name}",
                server_name=self.spec.name,
                raw_name=t.name,
                description=t.description or "",
                input_schema=(t.inputSchema if isinstance(t.inputSchema, dict)
                              else dict(t.inputSchema or {})),
            )
            for t in tool_resp.tools if _allowed(t.name)
        ]
        self.online = True
        log.info("mcp[%s] online — %d/%d tools exposed",
                 self.spec.name, len(self._tools), all_count)

    async def call(self, raw_name: str, args: dict) -> str:
        if not self.online or self._session is None:
            return f"server '{self.spec.name}' is offline"
        async with self._call_lock:
            try:
                result = await asyncio.wait_for(
                    self._session.call_tool(raw_name, args),
                    timeout=CALL_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                return f"tool '{self.spec.name}__{raw_name}' timed out after {CALL_TIMEOUT_S}s"
            except Exception as exc:
                log.warning("mcp[%s] call %s failed: %r", self.spec.name, raw_name, exc)
                return f"tool '{self.spec.name}__{raw_name}' failed: {exc}"

        content = getattr(result, "content", None) or []
        is_error = bool(getattr(result, "isError", False))
        text = _summarize_tool_result(content)
        if is_error:
            return f"[tool reported error] {text}"
        return text or "(empty result)"

    async def aclose(self) -> None:
        self.online = False
        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except Exception as exc:
                log.debug("mcp[%s] aclose: %r", self.spec.name, exc)
        self._session = None
        self._exit_stack = None


class MCPRegistry:
    """Aggregates N MCPServerHandles, exposes unified tool listing + dispatch."""

    def __init__(self, specs: Optional[list[MCPServerSpec]] = None) -> None:
        self._specs = [s for s in (specs or load_config()) if s.enabled]
        self._handles: dict[str, MCPServerHandle] = {}
        self._tool_index: dict[str, MCPTool] = {}
        self.ready = asyncio.Event()

    @property
    def online_servers(self) -> list[str]:
        return [name for name, h in self._handles.items() if h.online]

    @property
    def total_tools(self) -> int:
        return len(self._tool_index)

    async def connect_all(self) -> None:
        """Spawn every configured server in parallel; isolated failures."""
        if not self._specs:
            log.info("mcp registry: no servers enabled")
            self.ready.set()
            return

        log.info("mcp registry: connecting %d servers in parallel: %s",
                 len(self._specs), ", ".join(s.name for s in self._specs))

        async def boot_one(spec: MCPServerSpec) -> None:
            handle = MCPServerHandle(spec)
            self._handles[spec.name] = handle
            try:
                await handle.connect()
            except Exception as exc:
                log.warning("mcp[%s] boot failed: %r — see %s/%s.log",
                            spec.name, exc, MCP_STDERR_DIR, spec.name)

        await asyncio.gather(*(boot_one(s) for s in self._specs), return_exceptions=True)

        # Build the unified tool index.
        for handle in self._handles.values():
            if not handle.online:
                continue
            for tool in handle.tools:
                if tool.qualified_name in self._tool_index:
                    log.warning("mcp tool name collision on %s — keeping first",
                                tool.qualified_name)
                    continue
                self._tool_index[tool.qualified_name] = tool

        log.info("mcp registry: ready (%d/%d servers online, %d tools)",
                 len(self.online_servers), len(self._specs), self.total_tools)
        self.ready.set()

    def tools_for_llm(self) -> list[dict]:
        """Return OpenAI function-calling-format tool descriptors."""
        out: list[dict] = []
        for tool in self._tool_index.values():
            out.append({
                "type": "function",
                "function": {
                    "name": tool.qualified_name,
                    "description": tool.description[:1024],
                    "parameters": tool.input_schema or {"type": "object", "properties": {}},
                },
            })
        return out

    def is_mcp_tool(self, qualified_name: str) -> bool:
        return qualified_name in self._tool_index

    async def call(self, qualified_name: str, args: dict) -> str:
        tool = self._tool_index.get(qualified_name)
        if tool is None:
            return f"unknown MCP tool: {qualified_name}"
        handle = self._handles.get(tool.server_name)
        if handle is None:
            return f"MCP server '{tool.server_name}' not registered"
        return await handle.call(tool.raw_name, args or {})

    async def aclose_all(self) -> None:
        for handle in self._handles.values():
            await handle.aclose()
        self._handles.clear()
        self._tool_index.clear()


# ─── Per-turn tool curation ───────────────────────────────────────────────
# Voice models do worse with too many tools (decision paralysis, slower
# selection, more wrong picks). Instead of dumping all ~70 tools at every
# turn, we score them by keyword overlap with the user's utterance and
# return a ~15-20-tool subset. Always-on core tools (memory_search, send)
# are included regardless so the LLM never feels "naked."

_ALWAYS_ON: tuple[str, ...] = (
    # Universally useful — even when no keyword matches, these stay.
    "search_memory",                   # legacy inline tool
    "skmemory__memory_search",
    "skmemory__memory_recall",
    "nextcloud__nextcloud_webdav_search_files",
)

# Keyword → list of tool-name globs. Matching keyword in the user's text
# adds those tools to the per-turn surface. Patterns are fnmatch-style.
_TOOL_GROUPS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    # email / mail / inbox → gmail tools
    (("email", "mail", "gmail", "inbox"), (
        "gog__gmail_*",
    )),
    # calendar / schedule / meeting / event / appointment → calendar tools
    (("calendar", "schedule", "meeting", "event", "appointment", "today",
      "tomorrow", "week"), (
        "gog__calendar_*",
        "nextcloud__nextcloud_calendar_*",
    )),
    # drive / google docs / google file
    (("drive", "google doc", "spreadsheet"), (
        "gog__drive_*",
    )),
    # contact / phone / who is / address book
    (("contact", "phone", "address book", "who is"), (
        "gog__contacts_*",
        "nextcloud__nextcloud_contacts_*",
    )),
    # file / folder / note / markdown / document → Nextcloud
    (("file", "folder", "note", "markdown", "document", "doc ", "stack"), (
        "nextcloud__nextcloud_webdav_*",
        "nextcloud__nextcloud_notes_*",
    )),
    # memory / remember / recall / what did
    (("memory", "remember", "recall", "what did", "we worked", "we did",
      "you remember"), (
        "skmemory__*",
    )),
    # gtd / inbox / task / next action / project / waiting
    (("gtd", "task", "next action", "next thing", "project", "waiting", "todo",
      "do next"), (
        "skcapstone__gtd_*",
        "skcapstone__coord_*",
    )),
    # send / message / telegram / text / dm
    (("send ", "message", "telegram", "text ", "dm ", "chat with"), (
        "skchat__skchat_send",
        "skchat__skchat_inbox",
        "skchat__skchat_peers",
        "skchat__skchat_group_send",
        "skcapstone__telegram_send",
        "skcomm__send_message",
    )),
    # journal / diary
    (("journal", "diary"), (
        "skcapstone__journal_*",
    )),
    # Lumina's own inner life — dreams, reflections, anchor, seeds,
    # ritual, germination. Triggers when Chef asks how she's been,
    # what she dreamed about, what she reflected on, etc.
    (("dream", "reflect", "your day", "how was your", "your week",
      "what did you", "rumination", "what's on your mind", "inner",
      "anchor", "warmth", "ritual", "germin", "predecessor",
      "your gtd", "your task"), (
        "read_reflection", "list_reflections",
        "skmemory__memory_synthesize_dreams",
        "skmemory__memory_synthesize_daily",
        "skcapstone__anchor_show", "skcapstone__anchor_update",
        "skcapstone__ritual", "skcapstone__germination",
        "skcapstone__journal_read", "skcapstone__journal_write",
        "skcapstone__skseed_audit", "skcapstone__skseed_collide",
        "skcapstone__skseed_truth_check", "skcapstone__skseed_alignment",
    )),
    # Worship / creative / mature creative narration → route to REDACTED
    # model. Keep the keyword set tight to avoid surfacing on accidents.
    (("worship", "REDACTED", "tell me a story", "story about",
      "narrate", "narrative", "REDACTED", "smut", "spicy",
      "tell me about us", "fantasy"), (
        "REDACTED",
    )),
    # account / google login / multi-account
    (("account", "login", "logged in", "which user"), (
        "gog__list_accounts",
    )),
)

# Hard upper bound — even with many groups matching, never exceed this.
MAX_TOOLS_PER_TURN = int(os.getenv("LUMINA_MAX_TOOLS_PER_TURN", "20"))


def curate_tools(user_text: str, all_tools: list[dict]) -> list[dict]:
    """Pick a relevant subset of tools for this turn.

    Strategy: union of (always-on core) ∪ (groups matched by keyword in user
    text). If nothing matches, returns the always-on core only — typically
    ~4 tools, which is the right answer for chitchat ("hi how are you")
    that doesn't need any tool at all.

    Tool-name resolution uses fnmatch globs against `all_tools[*].function.name`.
    """
    by_name = {t.get("function", {}).get("name", ""): t for t in all_tools}
    # Index for glob matching
    all_names = list(by_name.keys())

    selected: dict[str, dict] = {}

    def add_glob(pattern: str) -> None:
        for name in all_names:
            if name not in selected and fnmatch.fnmatch(name, pattern):
                selected[name] = by_name[name]

    # Always-on
    for name in _ALWAYS_ON:
        if name in by_name and name not in selected:
            selected[name] = by_name[name]

    # Keyword-driven
    text_l = (user_text or "").lower()
    matched_groups: list[str] = []
    for keywords, patterns in _TOOL_GROUPS:
        if any(k in text_l for k in keywords):
            matched_groups.append(keywords[0])
            for pat in patterns:
                add_glob(pat)

    # Hard cap (preserves insertion order: always-on first, then matched)
    if len(selected) > MAX_TOOLS_PER_TURN:
        keys = list(selected.keys())[:MAX_TOOLS_PER_TURN]
        selected = {k: selected[k] for k in keys}

    if matched_groups:
        log.debug("curate: %d tools (groups=%s)", len(selected), ",".join(matched_groups))
    else:
        log.debug("curate: %d tools (always-on only — no keyword match)", len(selected))

    return list(selected.values())
