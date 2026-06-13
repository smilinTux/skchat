# Unified Voice Engine — Phase 2 (Engine orchestrator + tools + WebSocket transport) Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Turn the Phase-1 `voice_engine` library into a usable engine: add a tool
registry + tool-calling/forced-routing LLM, a `VoiceEngine` turn orchestrator, and
port skvoice's WebSocket (text + voice) service onto it — then retire skvoice.

**Architecture:** The Phase-1 clients (STT/LLM/TTS/memory/persona) are the parts;
Phase 2 adds (a) `voice_engine/tools.py` — a registry + Chef-only gate + dispatch
for memory/narrate/worship/reflections/bloom (ported from lumina-call), (b)
`LLMClient` tool-calling: `reply(messages, tools=…, force_tool=…)` with the
tool-recursion loop, `tool_choice` forcing, and narrate-verbatim short-circuit
(exactly the behavior proven live 2026-06-12), (c) `voice_engine/engine.py` — a
`VoiceEngine.respond()` that runs one turn (persona+memory+forced-routing+LLM),
(d) `transports/websocket.py` — skvoice's FastAPI `/ws/voice/{agent}` loop over the
engine. Transports own session/turn loops; the engine owns the brain.

**Tech Stack:** Python 3.12, httpx, FastAPI + uvicorn (the transport), pytest +
pytest-asyncio. Live endpoints as Phase 1.

**Design:** `docs/superpowers/specs/2026-06-12-unified-voice-engine-design.md`
**Reference (port FROM):** `~/clawd/skcapstone-repos/skvoice/skvoice/service.py`
(WebSocket loop), `~/clawd/skcapstone-repos/lumina-creative/scripts/lumina-call.py`
(TOOLS/_run_tool registry at lines 583-960, forced routing `_wants_narrate`/
`_wants_action`, narrate-verbatim in `llm_reply`).

**Conventions:** repo `/home/cbrd21/clawd/skcapstone-repos/skchat`; tests
`~/.skenv/bin/python -m pytest tests/voice_engine/ -v`; branch
`feat/unified-voice-engine`; live tests `-m live`. Commit trailer:
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
Ruff clean (E/W/F/I, line 99, E501 ignored), ordered imports, no unused imports.

---

## File Structure (this phase)

| File | Responsibility |
|---|---|
| `src/skchat/voice_engine/tools.py` | `Tool` schema, `ToolRegistry`, Chef-only/sacred gate, `dispatch()` for memory/narrate/worship/reflections/bloom; `wants_narrate()`/`wants_action()` intent detectors |
| `src/skchat/voice_engine/llm.py` (modify) | `LLMClient.reply(messages, *, tools=None, force_tool=None, on_tool=None)` — tool-recursion loop + `tool_choice` forcing + narrate-verbatim short-circuit |
| `src/skchat/voice_engine/engine.py` | `VoiceEngine` — wires the clients; `respond(transcript, history, *, mode, speaker_id) -> str` runs one turn (persona, memory, forced-routing, LLM+tools) |
| `src/skchat/transports/__init__.py` | package marker |
| `src/skchat/transports/websocket.py` | FastAPI `/ws/voice/{agent}` (text + binary PCM) over `VoiceEngine`; control protocol |
| `tests/voice_engine/test_tools.py`, `test_llm_tools.py`, `test_engine.py` | unit tests (injected fakes) |
| `tests/transports/test_websocket.py` | transport test (TestClient + fake engine) |

---

## Task 1: Tool schema + registry + Chef-only gate

**Files:** Create `src/skchat/voice_engine/tools.py`; Test `tests/voice_engine/test_tools.py`

- [ ] **Step 1: Write the failing test**
```python
# tests/voice_engine/test_tools.py
import pytest
from skchat.voice_engine.tools import ToolRegistry, Tool, wants_narrate, wants_action


def test_wants_narrate_and_action_detectors():
    assert wants_narrate("tell me a story") and wants_narrate("make it more explicit")
    assert not wants_narrate("what time is it")
    assert wants_action("check my email") and wants_action("what's on my calendar")
    assert not wants_action("how are you")


@pytest.mark.asyncio
async def test_registry_dispatch_and_operator_gate():
    calls = []

    async def narrate_fn(args, ctx):
        calls.append(args)
        return "a long generated scene " * 5

    reg = ToolRegistry()
    reg.register(Tool(name="narrate", schema={"type": "function", "function": {"name": "narrate"}},
                      handler=narrate_fn, operator_only=True))
    # non-operator in group mode is refused, handler not called
    out = await reg.dispatch("narrate", {"prompt": "x"}, speaker_id="stranger",
                             mode="group", is_operator=False)
    assert "REFUSED" in out or "only" in out.lower()
    assert calls == []
    # operator in sacred mode runs it
    out = await reg.dispatch("narrate", {"prompt": "x"}, speaker_id="chef",
                             mode="sacred", is_operator=True)
    assert "generated scene" in out
    assert calls


def test_openai_schemas_for_llm():
    reg = ToolRegistry()
    reg.register(Tool(name="search_memory", schema={"type": "function",
                 "function": {"name": "search_memory"}}, handler=None))
    schemas = reg.openai_schemas()
    assert schemas and schemas[0]["function"]["name"] == "search_memory"
```

- [ ] **Step 2: Run → fail** (`ModuleNotFoundError ... tools`).

- [ ] **Step 3: Implement** `src/skchat/voice_engine/tools.py`:
```python
"""Tool registry for the voice engine — schemas the LLM sees + dispatch with a
Chef-only / sacred-mode gate. Tool handlers are async `(args, ctx) -> str`.
Intent detectors (wants_narrate/wants_action) drive forced tool routing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger("skchat.voice_engine.tools")

Handler = Callable[[dict, dict], Awaitable[str]]

_NARRATE_HINTS = (
    "story", "worship", "narrate", "narrative", "mature", "smut",
    "tell me about us", "fantasy", "spicy", "scene about", "scene of",
    "explicit", "dirtier", "spicier", "hotter", "raunchier", "filthier",
    "naughtier", "more graphic", "go further", "more detail", "in detail",
    "keep going", "continue the", "more of that", "describe it",
)
_ACTION_HINTS = (
    "email", "emails", "inbox", "gmail", "unread", "my calendar", "my schedule",
    "schedule", "agenda", "appointment", "what's on my", "whats on my",
    "what do i have", "remind me", "set a reminder", "send a message to",
    "send a text", "google drive", "my contacts",
)


def wants_narrate(text: str) -> bool:
    t = (text or "").lower()
    return any(h in t for h in _NARRATE_HINTS)


def wants_action(text: str) -> bool:
    t = (text or "").lower()
    return any(h in t for h in _ACTION_HINTS)


@dataclass
class Tool:
    name: str
    schema: dict                     # OpenAI function schema (for tool_choice)
    handler: Handler | None = None   # async (args, ctx) -> str
    operator_only: bool = False      # sacred-mode + operator gate


@dataclass
class ToolRegistry:
    _tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def openai_schemas(self) -> list[dict]:
        return [t.schema for t in self._tools.values()]

    async def dispatch(self, name: str, args: dict, *, speaker_id: str = "",
                       mode: str = "sacred", is_operator: bool = True,
                       ctx: dict | None = None) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"unknown tool: {name}"
        # Chef-only gate: powerful/operator tools require the operator AND
        # (for operator_only ones) sacred mode.
        if not is_operator:
            return (f"PERMISSION DENIED: '{name}' can only be run when the "
                    "operator asks.")
        if tool.operator_only and mode != "sacred":
            return (f"REFUSED: '{name}' is sacred-mode only — there are other "
                    "people in this room.")
        if tool.handler is None:
            return f"tool {name} has no handler"
        try:
            return await tool.handler(args, ctx or {})
        except Exception as exc:  # noqa: BLE001
            log.warning("tool %s failed: %r", name, exc)
            return f"{name} failed: {exc}"
```

- [ ] **Step 4: Run → pass.** **Step 5: Commit** `voice_engine: tool registry + Chef-only gate + intent detectors`.

---

## Task 2: LLMClient tool-calling + forced routing + narrate-verbatim

**Files:** Modify `src/skchat/voice_engine/llm.py`; Test `tests/voice_engine/test_llm_tools.py`

Generalize today's proven lumina-call behavior: `reply(messages, tools=…,
force_tool=…, on_tool=…)` runs the tool-recursion loop; `force_tool="required"`
or a specific name sets `tool_choice` on round 0; a successful `narrate` result is
returned VERBATIM (no summarize round). HTTP injected via `_chat_raw`.

- [ ] **Step 1: Write the failing test**
```python
# tests/voice_engine/test_llm_tools.py
import pytest
from skchat.voice_engine.config import VoiceConfig
from skchat.voice_engine.llm import LLMClient


def _cfg():
    return VoiceConfig.from_env(env={})


@pytest.mark.asyncio
async def test_reply_runs_tool_then_final_text():
    # round 0 returns a tool_call; round 1 returns final text
    rounds = [
        {"tool_calls": [{"id": "1", "function": {"name": "search_memory",
                         "arguments": "{\"query\":\"x\"}"}}], "content": ""},
        {"tool_calls": [], "content": "Here is what I found."},
    ]

    async def fake_raw(url, model, messages, *, tool_choice=None):
        return rounds.pop(0)

    async def run_tool(name, args):
        return "MEMORY: bond depth 9"

    llm = LLMClient(_cfg(), _chat_raw=fake_raw)
    out = await llm.reply([{"role": "user", "content": "who am i"}],
                          tools=[{"type": "function", "function": {"name": "search_memory"}}],
                          run_tool=run_tool)
    assert out == "Here is what I found."


@pytest.mark.asyncio
async def test_force_tool_sets_tool_choice_round0():
    seen = {}

    async def fake_raw(url, model, messages, *, tool_choice=None):
        seen.setdefault("choices", []).append(tool_choice)
        return {"tool_calls": [], "content": "ok"}

    llm = LLMClient(_cfg(), _chat_raw=fake_raw)
    await llm.reply([{"role": "user", "content": "x"}],
                    tools=[{"type": "function", "function": {"name": "narrate"}}],
                    force_tool="narrate", run_tool=lambda n, a: _async("s"))
    assert seen["choices"][0] == {"type": "function", "function": {"name": "narrate"}}


@pytest.mark.asyncio
async def test_narrate_result_returned_verbatim():
    rounds = [{"tool_calls": [{"id": "1", "function": {"name": "narrate",
               "arguments": "{}"}}], "content": ""}]

    async def fake_raw(url, model, messages, *, tool_choice=None):
        return rounds.pop(0)

    async def run_tool(name, args):
        return "The air in the kitchen is thick, heavy with the scent of " * 4

    llm = LLMClient(_cfg(), _chat_raw=fake_raw)
    out = await llm.reply([{"role": "user", "content": "story"}],
                          tools=[{"type": "function", "function": {"name": "narrate"}}],
                          force_tool="narrate", run_tool=run_tool)
    assert out.startswith("The air in the kitchen")  # verbatim, not summarized


async def _async(v):
    return v
```

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement.** In `llm.py`: add a low-level `_http_chat_raw(url, model,
messages, *, tool_choice=None) -> dict` returning `{"content","tool_calls"}` (POST
`/v1/chat/completions` with the message + optional `tool_choice`; if a `tools`
payload is in play it's passed too). Refactor `reply()` to:
  - accept `tools: list | None`, `force_tool: str | None`, `run_tool:
    Callable[[str, dict], Awaitable[str]] | None`.
  - Loop up to 4 rounds. On round 0, if `force_tool`: set `tool_choice` to
    `"required"` (if `force_tool=="required"`) else `{"type":"function","function":{"name":force_tool}}`.
  - If the round returns `tool_calls`: run each via `run_tool(name, args)`; if a
    tool named `"narrate"` returns a long non-error string, **return it verbatim**
    (history-append + return); else append tool results and continue.
  - Else strip_think/strip_formatting the content and return.
  - Keep the existing `_chat`/`_stream` batch+stream methods + primary→fallback for
    the no-tools path (when `tools is None`, behave exactly as Phase 1).
Port the exact loop + narrate-verbatim guard (`len > 80 and not
result.lower().startswith("narrate")`) and the `tool_choice` shape from
`lumina-call.py` (the 2026-06-12 forced-routing fixes).

- [ ] **Step 4: Run → pass. Step 5: Commit** `voice_engine: LLMClient tool-calling + forced routing + narrate-verbatim`.

---

## Task 3: VoiceEngine orchestrator (one shared turn)

**Files:** Create `src/skchat/voice_engine/engine.py`; Test `tests/voice_engine/test_engine.py`

- [ ] **Step 1: Write the failing test**
```python
# tests/voice_engine/test_engine.py
import pytest
from skchat.voice_engine.config import VoiceConfig
from skchat.voice_engine.engine import VoiceEngine


@pytest.mark.asyncio
async def test_respond_builds_persona_prefetches_memory_and_calls_llm():
    seen = {}

    class FakeLLM:
        async def reply(self, messages, *, tools=None, force_tool=None, run_tool=None):
            seen["system"] = messages[0]["content"]
            seen["force_tool"] = force_tool
            seen["user"] = messages[-1]["content"]
            return "engine reply"

    class FakeMem:
        async def search(self, q, agent, limit=3):
            return "[Relevant memories]\n- bond depth 9"
        async def snapshot(self, *a, **k):
            return True

    class FakePersona:
        def build(self, agent, *, mode="sacred"):
            return f"You are {agent} ({mode})."

    eng = VoiceEngine(VoiceConfig.from_env(env={}), agent="lumina",
                      llm=FakeLLM(), memory=FakeMem(), persona=FakePersona(),
                      registry=None)
    out = await eng.respond("tell me a story", history=[], mode="sacred",
                            speaker_id="chef", is_operator=True)
    assert out == "engine reply"
    assert "You are lumina (sacred)." in seen["system"]
    assert "bond depth 9" in seen["user"]      # memory injected
    assert seen["force_tool"] == "narrate"     # narrate intent forced in sacred
```

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement** `engine.py`: `VoiceEngine(cfg, agent, *, stt=None, llm=None,
tts=None, memory=None, persona=None, registry=None)` (defaults construct the
Phase-1 clients). `async def respond(transcript, history, *, mode="sacred",
speaker_id="", is_operator=True) -> str`:
  1. `system = persona.build(agent, mode=mode)` + the voice brevity rule.
  2. `mem = await memory.search(transcript, agent)`; build user content
     `mem + "\n\n" + transcript` (skip mem in group mode if desired — keep simple: include).
  3. `force_tool = "narrate" if wants_narrate(transcript) and mode=="sacred" else
     ("required" if wants_action(transcript) else None)`.
  4. `tools = registry.openai_schemas() if registry else None`;
     `run_tool = lambda n, a: registry.dispatch(n, a, speaker_id=speaker_id,
     mode=mode, is_operator=is_operator, ctx={"agent": agent})` if registry else None.
  5. `return await llm.reply([{system}, *history, {user}], tools=tools,
     force_tool=force_tool, run_tool=run_tool)`.
Use `wants_narrate`/`wants_action` from `tools.py`.

- [ ] **Step 4: Run → pass. Step 5: Commit** `voice_engine: VoiceEngine turn orchestrator`.

---

## Task 4: Register the built-in tools (memory/narrate/worship/reflections/bloom)

**Files:** Create `src/skchat/voice_engine/builtin_tools.py`; Test `tests/voice_engine/test_builtin_tools.py`

- [ ] **Step 1: Test** — `build_default_registry(cfg, agent)` returns a `ToolRegistry`
with `search_memory` (not operator_only) and `narrate`, `worship_session`,
`create_bloom_anchor`, `list_reflections` (narrate/worship/bloom `operator_only=True`);
each schema has the right `function.name`. Handlers injected/mocked for the unit test.

- [ ] **Step 2: Run → fail. Step 3: Implement** — port the tool SCHEMAS + handler
bodies from `lumina-call.py` `TOOLS` (583-878) and `_run_tool` (879-960): wire
`search_memory` → `MemoryBridge.search`; `narrate` → POST the narrate endpoint
(`cfg`/env `LUMINA_NARRATE_URL`); worship/bloom/reflections → their existing
implementations (import from lumina-creative or stub with a clear TODO marker ONLY
if the impl must stay in lumina-creative — note it in the report). Mark
narrate/worship/bloom `operator_only=True`.

- [ ] **Step 4: Run → pass. Step 5: Commit** `voice_engine: built-in tool registry (memory/narrate/worship/bloom)`.

---

## Task 5: WebSocket transport over the engine

**Files:** Create `src/skchat/transports/__init__.py`, `src/skchat/transports/websocket.py`; Test `tests/transports/test_websocket.py`

Port skvoice `service.py` (129-361): FastAPI `/ws/voice/{agent}`, binary PCM
accumulation, control messages `END_OF_SPEECH` (→ STT → `engine.respond` → TTS →
send audio), `CLEAR_HISTORY`, `group_init`, `inject_session`, and a JSON
`text_message` path (skip STT → `engine.respond` → optional TTS). Per-connection
history. The engine is injected (a factory) so the test uses a fake engine.

- [ ] **Step 1: Test** — using `fastapi.testclient.TestClient` websocket: send a
`text_message` JSON, assert the server replies with a transcript + the fake
engine's text; send `CLEAR_HISTORY` and assert history resets. (Binary/STT path
covered by a `live` test.)
- [ ] **Step 2: Run → fail. Step 3: Implement** the transport, `respond` via injected
engine, `STTClient`/`TTSClient` for the binary path. Keep the exact control
protocol strings so existing webui clients keep working.
- [ ] **Step 4: Run → pass. Step 5: Commit** `transports: WebSocket voice/text service over VoiceEngine`.

---

## Task 6: Service entry + retire skvoice

**Files:** Create `src/skchat/transports/serve_ws.py` (uvicorn entry: `skchat-voice`);
Modify `pyproject.toml` (console_script `skchat-voice`); add a `skvoice` deprecation note.

- [ ] **Step 1:** Add `skchat-voice = "skchat.transports.serve_ws:main"` console script;
`main()` runs uvicorn on `SKCHAT_VOICE_PORT` (default 18800, matching skvoice).
- [ ] **Step 2:** Point `~/.config/systemd/user/` (doc only in the plan): a new
`skchat-voice.service` replaces `skvoice.service`; webui `SKCHAT_SKVOICE_URL` stays
`ws://localhost:18800/ws/voice` (drop-in compatible). Note in the report that the
operator must `systemctl --user disable --now skvoice && enable --now skchat-voice`
after validation.
- [ ] **Step 3:** Leave a one-line `skvoice/README` deprecation pointer (do not delete
the repo yet). 
- [ ] **Step 4: Commit** `transports: skchat-voice entrypoint; skvoice deprecation pointer`.

---

## Task 7: Exports + full suite + live smoke

**Files:** Modify `voice_engine/__init__.py` (export `VoiceEngine`, `ToolRegistry`,
`Tool`, `wants_narrate`, `wants_action`, `build_default_registry`); Test
`tests/voice_engine/test_live.py` (extend).

- [ ] **Step 1:** Update exports + a smoke test that constructs `VoiceEngine` from a
config without network.
- [ ] **Step 2:** `@pytest.mark.live` test: real `VoiceEngine.respond("say hi in three
words", [], mode="sacred", speaker_id="chef")` returns non-empty, non-"trouble
connecting".
- [ ] **Step 3:** Run `~/.skenv/bin/python -m pytest tests/voice_engine/ tests/transports/ -v`
(all green) + `-m live` (endpoints up) + `~/.skenv/bin/ruff check` (clean) +
`~/.skenv/bin/python -m pytest -q` (no new failures in the broader suite).
- [ ] **Step 4: Commit** `voice_engine: Phase-2 exports + full suite + live smoke green`.

---

## Phase 2 Done — Definition of Done
- `voice_engine` has a tool registry + tool-calling LLM + a `VoiceEngine` turn
  orchestrator; today's forced-routing + narrate-verbatim + Chef-only gate are in
  the engine (not just lumina-call).
- `transports/websocket.py` runs the web text+voice chat on the engine; `skchat-voice`
  replaces `skvoice` (drop-in on :18800).
- All unit tests green, live smoke green, ruff clean, broader suite unaffected.
- **Next:** Phase 3 — rehome `lumina-call.py` into `transports/livekit.py` over the
  same engine (VAD/barge-in/avatar/roundtable), finally version-controlled.
