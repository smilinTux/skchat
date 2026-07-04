# SKChat Group Chat (Chef + Lumina + Opus) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A native skchat group where each agent (Lumina, Opus) replies in its own soul+memory when `@`-mentioned, generating via skgateway (`reg:ornith`).

**Architecture:** New `GroupResponder` (per-agent) generalizes `advocacy.py`: mention-check → soul+FEB system prompt (skcapstone `SystemPromptBuilder`) → skmemory recall → skgateway `/v1/chat/completions` → `GroupChat.send` reply → skmemory store. Wired into both agent daemons for group messages. Talk-first MVP (no tool-loop).

**Tech Stack:** Python 3.10+ (skchat, `~/.skenv`), pytest, httpx, skcapstone (`SystemPromptBuilder`/`load_consciousness_config`), skmemory (`MemoryStore`), skgateway (OpenAI-compatible gateway on `:18780`).

**Spec:** `docs/superpowers/specs/2026-07-04-skchat-group-chat-design.md`

## Global Constraints

- Run tests from `~` to avoid the skmemory namespace collision: `cd ~ && ~/.skenv/bin/python -m pytest <path> -q`.
- Line length 99; target Python 3.10+; ruff (E,W,F,I; ignore E501).
- **Native/sovereign:** no Hermes imports. Only skcapstone, skmemory, httpx, stdlib.
- Backend is a **per-agent config knob** — never hardcode a model/URL in logic. Prod `model=reg:ornith`, `backend_url=http://localhost:18780/v1/chat/completions`. Dev may use any live skgateway model.
- **Loop safety:** an agent replies ONLY to explicit mentions of itself/`@all`, never to its own messages, never to another agent's un-mentioned line.
- Editable install — code is live on daemon restart; never restart a daemon mid-render. Restart with `systemctl --user restart skchat-daemon skchat-daemon-opus`.
- Commit after each task.

---

### Task 1: `GroupResponderConfig` — per-agent config

**Files:**
- Create: `src/skchat/group_responder.py`
- Test: `tests/test_group_responder.py`

**Interfaces:**
- Produces: `GroupResponderConfig` dataclass with fields `agent: str`, `mentions: list[str]`, `groups: list[str]`, `backend_url: str`, `model: str`, `history_turns: int`, `max_reply_tokens: int`, `on_error: str`; and `load_group_config(agent: str, env: Mapping | None = None) -> GroupResponderConfig`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_group_responder.py
from skchat.group_responder import GroupResponderConfig, load_group_config


def test_config_defaults_for_lumina():
    cfg = load_group_config("lumina", env={})
    assert cfg.agent == "lumina"
    assert cfg.backend_url == "http://localhost:18780/v1/chat/completions"
    assert cfg.model == "reg:ornith"
    # self-mentions include the agent name; @all/@both always match
    assert "@lumina" in cfg.mentions
    assert "@all" in cfg.mentions and "@both" in cfg.mentions
    assert cfg.on_error == "silent"


def test_config_env_overrides():
    cfg = load_group_config("opus", env={
        "SKCHAT_GROUP_BACKEND_URL": "http://localhost:8082/v1/chat/completions",
        "SKCHAT_GROUP_MODEL": "qwen3.6-27b-abliterated",
        "SKCHAT_GROUPS": "group:abc,group:def",
    })
    assert cfg.agent == "opus"
    assert "@opus" in cfg.mentions
    assert cfg.model == "qwen3.6-27b-abliterated"
    assert cfg.groups == ["group:abc", "group:def"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skchat/tests/test_group_responder.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'skchat.group_responder'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/skchat/group_responder.py
"""GroupResponder — native per-agent skchat group auto-responder.

Generalizes advocacy.py: when THIS agent is @-mentioned in a group message,
build its soul+FEB prompt (skcapstone), recall memory (skmemory), generate via
skgateway (reg:ornith), and return the reply. Talk-first (no tool-loop).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional

_DEFAULT_BACKEND = "http://localhost:18780/v1/chat/completions"
_DEFAULT_MODEL = "reg:ornith"
# mentions that address every agent in the room
_BROADCAST_MENTIONS = ["@all", "@both", "@everyone"]


@dataclass
class GroupResponderConfig:
    agent: str
    mentions: list[str]
    groups: list[str] = field(default_factory=list)
    backend_url: str = _DEFAULT_BACKEND
    model: str = _DEFAULT_MODEL
    history_turns: int = 8
    max_reply_tokens: int = 800
    on_error: str = "silent"  # "silent" | "note"


def load_group_config(agent: str, env: Optional[Mapping[str, str]] = None) -> GroupResponderConfig:
    """Build a config for *agent* from env (SKCHAT_GROUP_*) with SKWorld defaults."""
    if env is None:
        env = os.environ
    agent = (agent or "lumina").strip().lower()
    mentions = [f"@{agent}"] + _BROADCAST_MENTIONS
    groups_raw = (env.get("SKCHAT_GROUPS") or "").strip()
    groups = [g.strip() for g in groups_raw.split(",") if g.strip()]
    return GroupResponderConfig(
        agent=agent,
        mentions=mentions,
        groups=groups,
        backend_url=(env.get("SKCHAT_GROUP_BACKEND_URL") or _DEFAULT_BACKEND).strip(),
        model=(env.get("SKCHAT_GROUP_MODEL") or _DEFAULT_MODEL).strip(),
        history_turns=int(env.get("SKCHAT_GROUP_HISTORY_TURNS") or 8),
        max_reply_tokens=int(env.get("SKCHAT_GROUP_MAX_TOKENS") or 800),
        on_error=(env.get("SKCHAT_GROUP_ON_ERROR") or "silent").strip(),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skchat/tests/test_group_responder.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/clawd/skcapstone-repos/skchat
git add src/skchat/group_responder.py tests/test_group_responder.py
git commit -m "feat(group): GroupResponderConfig + per-agent env loader"
```

---

### Task 2: `should_respond` — per-agent mention gate

**Files:**
- Modify: `src/skchat/group_responder.py`
- Test: `tests/test_group_responder.py`

**Interfaces:**
- Consumes: `GroupResponderConfig` (Task 1); `_token_match` from `skchat.advocacy`.
- Produces: `should_respond(content: str, sender: str, cfg: GroupResponderConfig) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_group_responder.py
from skchat.group_responder import should_respond, load_group_config

_LUM = load_group_config("lumina", env={})


def test_should_respond_matrix():
    # addressed to me -> yes
    assert should_respond("@lumina hi", "chef@skworld.io", _LUM) is True
    # @all -> yes
    assert should_respond("@all standup?", "chef@skworld.io", _LUM) is True
    # addressed to the OTHER agent only -> no
    assert should_respond("@opus thoughts?", "chef@skworld.io", _LUM) is False
    # no mention -> no
    assert should_respond("just thinking out loud", "chef@skworld.io", _LUM) is False
    # my own message (loop guard) even if it contains @lumina -> no
    assert should_respond("@lumina echo", "capauth:lumina@skworld.io", _LUM) is False
    assert should_respond("@all echo", "lumina@chef.skworld.io", _LUM) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skchat/tests/test_group_responder.py::test_should_respond_matrix -q`
Expected: FAIL — `ImportError: cannot import name 'should_respond'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/skchat/group_responder.py
from .advocacy import _token_match


def _is_self(sender: str, agent: str) -> bool:
    """True when *sender* is this agent (any of its identity forms)."""
    s = (sender or "").lower()
    # matches capauth:opus@skworld.io, opus@chef.skworld.io, opus, etc.
    handle = s.split(":", 1)[-1].split("@", 1)[0]
    return handle == agent


def should_respond(content: str, sender: str, cfg: GroupResponderConfig) -> bool:
    """True iff this agent is explicitly addressed and the sender is not itself."""
    if _is_self(sender, cfg.agent):
        return False
    low = (content or "").lower()
    return any(_token_match(low, m) for m in cfg.mentions)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skchat/tests/test_group_responder.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/clawd/skcapstone-repos/skchat
git add src/skchat/group_responder.py tests/test_group_responder.py
git commit -m "feat(group): should_respond per-agent mention gate + self loop-guard"
```

---

### Task 3: `generate` — skgateway call (injectable HTTP)

**Files:**
- Modify: `src/skchat/group_responder.py`
- Test: `tests/test_group_responder.py`

**Interfaces:**
- Consumes: `GroupResponderConfig` (Task 1).
- Produces: `generate(messages: list[dict], cfg: GroupResponderConfig, http=None) -> str | None`. Returns reply text, or `None` on failure. `http` is any object with `post(url, json=, timeout=)` returning a response with `status_code` + `json()`; defaults to a real `httpx.Client`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_group_responder.py
from skchat.group_responder import generate


class _Resp:
    def __init__(self, code, data): self.status_code, self._d = code, data
    def json(self): return self._d


class _Http:
    def __init__(self, resp): self._resp, self.calls = resp, []
    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json)); return self._resp


def test_generate_ok():
    http = _Http(_Resp(200, {"choices": [{"message": {"content": "Hey Chef \U0001f427"}}]}))
    out = generate([{"role": "user", "content": "hi"}], _LUM, http=http)
    assert out == "Hey Chef \U0001f427"
    url, payload = http.calls[0]
    assert url == _LUM.backend_url
    assert payload["model"] == "reg:ornith"
    assert payload["messages"][0]["content"] == "hi"


def test_generate_http_error_returns_none():
    http = _Http(_Resp(500, {"error": "boom"}))
    assert generate([{"role": "user", "content": "hi"}], _LUM, http=http) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skchat/tests/test_group_responder.py -k generate -q`
Expected: FAIL — `ImportError: cannot import name 'generate'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/skchat/group_responder.py
import logging
logger = logging.getLogger("skchat.group_responder")


def generate(messages: list[dict], cfg: GroupResponderConfig, http=None) -> Optional[str]:
    """POST an OpenAI-shaped chat completion to skgateway; return the reply text."""
    if http is None:  # pragma: no cover - real client, exercised live
        import httpx
        http = httpx.Client()
    payload = {
        "model": cfg.model,
        "messages": messages,
        "max_tokens": cfg.max_reply_tokens,
        "temperature": 0.8,
    }
    try:
        resp = http.post(cfg.backend_url, json=payload, timeout=120.0)
        if resp.status_code >= 400:
            logger.warning("group generate: skgateway HTTP %s", resp.status_code)
            return None
        data = resp.json() or {}
        return (data.get("choices") or [{}])[0].get("message", {}).get("content")
    except Exception as exc:
        logger.warning("group generate failed: %s", exc)
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skchat/tests/test_group_responder.py -k generate -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/clawd/skcapstone-repos/skchat
git add src/skchat/group_responder.py tests/test_group_responder.py
git commit -m "feat(group): generate() -> skgateway chat completion (injectable http)"
```

---

### Task 4: memory recall + store (best-effort)

**Files:**
- Modify: `src/skchat/group_responder.py`
- Test: `tests/test_group_responder.py`

**Interfaces:**
- Produces: `recall(query: str, store=None, limit: int = 5) -> str` (formatted context or `""`); `store_turn(user_text: str, reply: str, gid: str, store=None) -> None`. `store` is any object with `search(query, limit=...) -> list` and `snapshot(title, content, *, tags, source, source_ref)`; defaults to a lazily-built `skmemory.MemoryStore`. Both swallow all errors.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_group_responder.py
from skchat.group_responder import recall, store_turn


class _Mem:
    def __init__(self, hits=()): self._hits, self.snaps = list(hits), []
    def search(self, q, limit=5, **kw):
        return self._hits
    def snapshot(self, title, content, **kw): self.snaps.append((title, content, kw))


class _Hit:
    def __init__(self, c): self.content, self.title = c, "t"


def test_recall_formats_hits():
    mem = _Mem([_Hit("Chef likes teal"), _Hit("standup at 9")])
    out = recall("colors", store=mem)
    assert "Chef likes teal" in out and "standup at 9" in out


def test_recall_empty_on_error():
    class Boom:
        def search(self, *a, **k): raise RuntimeError("db down")
    assert recall("x", store=Boom()) == ""


def test_store_turn_snapshots():
    mem = _Mem()
    store_turn("q?", "a!", "group:xyz", store=mem)
    assert mem.snaps and mem.snaps[0][2]["source"] == "skchat"
    assert "group:xyz" in mem.snaps[0][2]["tags"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skchat/tests/test_group_responder.py -k "recall or store_turn" -q`
Expected: FAIL — `ImportError: cannot import name 'recall'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/skchat/group_responder.py
def _default_store():  # pragma: no cover - live skmemory
    from skmemory import MemoryStore
    return MemoryStore()


def recall(query: str, store=None, limit: int = 5) -> str:
    """Return a short 'Relevant memories' block for *query*, or '' on any failure."""
    if not (query or "").strip():
        return ""
    try:
        store = store or _default_store()
        hits = store.search(query, limit=limit)
    except Exception as exc:
        logger.debug("group recall failed: %s", exc)
        return ""
    lines = []
    for m in hits or []:
        c = (getattr(m, "content", "") or "")[:240]
        if c:
            lines.append(f"- {c}")
    return ("Relevant memories:\n" + "\n".join(lines)) if lines else ""


def store_turn(user_text: str, reply: str, gid: str, store=None) -> None:
    """Best-effort: snapshot the exchange to skmemory tagged with the group."""
    try:
        store = store or _default_store()
        title = (user_text or reply or "group turn").strip()[:60]
        content = f"User: {user_text}\n\nReply: {reply}".strip()[:4000]
        store.snapshot(
            title=title, content=content,
            tags=["skchat", f"{gid}"], source="skchat", source_ref=gid,
        )
    except Exception as exc:
        logger.debug("group store_turn failed: %s", exc)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skchat/tests/test_group_responder.py -k "recall or store_turn" -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/clawd/skcapstone-repos/skchat
git add src/skchat/group_responder.py tests/test_group_responder.py
git commit -m "feat(group): best-effort skmemory recall + store_turn"
```

---

### Task 5: `GroupResponder.respond` — orchestrate soul prompt → reply

**Files:**
- Modify: `src/skchat/group_responder.py`
- Test: `tests/test_group_responder.py`

**Interfaces:**
- Consumes: everything above; `skchat.models.ChatMessage`; skcapstone `SystemPromptBuilder` + `load_consciousness_config` (as used in `advocacy._call_consciousness`).
- Produces: `class GroupResponder` with `__init__(self, cfg, *, prompt_builder=None, http=None, store=None)` and `respond(self, msg: ChatMessage) -> str | None`. `prompt_builder` is any object with `build() -> str` (defaults to skcapstone's `SystemPromptBuilder`); returns reply text or `None` (not mentioned / generation failed).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_group_responder.py
from skchat.group_responder import GroupResponder
from skchat.models import ChatMessage


class _Builder:
    def build(self): return "You are Lumina. Warm, sovereign."


def _mk(content, sender="chef@skworld.io", recipient="group:room1"):
    return ChatMessage(sender=sender, recipient=recipient, content=content, thread_id="room1")


def test_respond_when_mentioned():
    http = _Http(_Resp(200, {"choices": [{"message": {"content": "teal, Chef."}}]}))
    r = GroupResponder(_LUM, prompt_builder=_Builder(), http=http, store=_Mem([_Hit("likes teal")]))
    out = r.respond(_mk("@lumina fav color?"))
    assert out == "teal, Chef."
    # system prompt + recall must be in the outbound messages
    _, payload = http.calls[0]
    roles = [m["role"] for m in payload["messages"]]
    assert roles[0] == "system"
    assert "Lumina" in payload["messages"][0]["content"]
    assert any("likes teal" in m["content"] for m in payload["messages"])


def test_respond_none_when_not_mentioned():
    http = _Http(_Resp(200, {"choices": [{"message": {"content": "x"}}]}))
    r = GroupResponder(_LUM, prompt_builder=_Builder(), http=http, store=_Mem())
    assert r.respond(_mk("@opus only you")) is None
    assert http.calls == []  # never hit the backend
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skchat/tests/test_group_responder.py -k respond -q`
Expected: FAIL — `ImportError: cannot import name 'GroupResponder'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/skchat/group_responder.py
class GroupResponder:
    """Per-agent group auto-responder: mention -> soul prompt -> recall -> generate."""

    def __init__(self, cfg: GroupResponderConfig, *, prompt_builder=None, http=None, store=None):
        self.cfg = cfg
        self._builder = prompt_builder
        self._http = http
        self._store = store

    def _system_prompt(self) -> str:
        if self._builder is not None:
            return self._builder.build()
        # live: skcapstone soul+FEB builder (same as advocacy._call_consciousness)
        from pathlib import Path  # pragma: no cover - live path
        from skcapstone.consciousness_config import load_consciousness_config
        from skcapstone.consciousness_loop import SystemPromptBuilder
        home = Path.home()
        config = load_consciousness_config(home)
        return SystemPromptBuilder(home, config.max_context_tokens).build()

    def respond(self, msg: ChatMessage) -> Optional[str]:
        if not should_respond(msg.content, msg.sender, self.cfg):
            return None
        system = self._system_prompt()
        mem = recall(msg.content[:200], store=self._store)
        user = f"{mem}\n\nMessage from {msg.sender}:\n{msg.content}" if mem else \
            f"Message from {msg.sender}:\n{msg.content}"
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user}]
        reply = generate(messages, self.cfg, http=self._http)
        if reply:
            gid = msg.thread_id or msg.recipient
            store_turn(msg.content, reply, gid, store=self._store)
        return reply
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skchat/tests/test_group_responder.py -q`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
cd ~/clawd/skcapstone-repos/skchat
git add src/skchat/group_responder.py tests/test_group_responder.py
git commit -m "feat(group): GroupResponder.respond orchestration (soul+recall+generate+store)"
```

---

### Task 6: Daemon integration — group messages → GroupResponder → group reply

**Files:**
- Modify: `src/skchat/daemon.py` (the receive loop that currently calls `engine.process_message(msg)`, ~line 362; and subsystem init ~line 272 where `AdvocacyEngine` is built)
- Test: `tests/test_daemon_group.py`

**Interfaces:**
- Consumes: `GroupResponder` (Task 5); `GroupChat.send(content, sender, transport=, history=)` from `skchat.group`; `load_group_config` (Task 1).
- Produces: daemon behavior — on a GROUP message (recipient starts with `group:` or thread_id matches a configured group) where the agent is mentioned, generate a reply and `GroupChat.send` it into the group as the agent. Guarded by `SKCHAT_GROUPS` non-empty. DMs keep the existing advocacy path.

- [ ] **Step 1: Write the failing test** (a focused unit around a new helper so we don't boot a full daemon)

```python
# tests/test_daemon_group.py
from skchat.group_responder import load_group_config, GroupResponder
from skchat.models import ChatMessage


class _Builder:
    def build(self): return "You are Lumina."


class _Resp:
    status_code = 200
    def json(self): return {"choices": [{"message": {"content": "hi from lumina"}}]}


class _Http:
    def __init__(self): self.calls = []
    def post(self, url, json=None, timeout=None): self.calls.append(json); return _Resp()


def test_group_message_routed_and_replied():
    # A group message mentioning lumina produces a reply the daemon would send.
    cfg = load_group_config("lumina", env={"SKCHAT_GROUPS": "group:room1"})
    r = GroupResponder(cfg, prompt_builder=_Builder(), http=_Http(), store=None)
    msg = ChatMessage(sender="chef@skworld.io", recipient="group:room1",
                      content="@lumina hi", thread_id="room1")
    reply = r.respond(msg)
    assert reply == "hi from lumina"


def test_dm_or_unmentioned_group_no_reply():
    cfg = load_group_config("lumina", env={"SKCHAT_GROUPS": "group:room1"})
    r = GroupResponder(cfg, prompt_builder=_Builder(), http=_Http(), store=None)
    msg = ChatMessage(sender="chef@skworld.io", recipient="group:room1",
                      content="@opus hi", thread_id="room1")
    assert r.respond(msg) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skchat/tests/test_daemon_group.py -q`
Expected: PASS on the respond logic (it reuses Task 5). If it errors on import, fix imports. (This test locks the contract the daemon wiring depends on.)

- [ ] **Step 3: Wire the daemon** — read `daemon.py` around the `engine` init (~272) and the receive loop (~362). Add, alongside the existing advocacy `engine`, a group path:

```python
# near where AdvocacyEngine is built (subsystem init):
from .group_responder import load_group_config, GroupResponder
group_cfg = load_group_config(os.environ.get("SKAGENT", "lumina"))
group_responder = GroupResponder(group_cfg) if group_cfg.groups else None
```

```python
# in the receive loop, BEFORE the existing `if engine:` DM-advocacy block:
if group_responder is not None and _is_group_message(msg, group_cfg.groups):
    try:
        reply = group_responder.respond(msg)
        if reply:
            from .group import GroupChat
            gid = (msg.thread_id or msg.recipient).replace("group:", "")
            grp = GroupChat.load(gid)  # existing @classmethod loader
            grp.send(reply, sender=self._identity, transport=skcomms, history=history)
            self.advocacy_responses += 1
    except Exception as exc:
        logger.warning("group responder failed: %s", exc)
    continue  # handled; don't also run DM advocacy
```

And add the helper near the top of `daemon.py`:

```python
def _is_group_message(msg, groups: list[str]) -> bool:
    rid = (getattr(msg, "recipient", "") or "")
    tid = (getattr(msg, "thread_id", "") or "")
    if not rid.startswith("group:") and not tid:
        return False
    key = rid if rid.startswith("group:") else f"group:{tid}"
    return (not groups) or (key in groups) or (f"group:{tid}" in groups)
```

> Verify against the real `daemon.py`: confirm `self._identity`, `skcomms`, `history` names in scope at the call site (they are used by the existing receive loop), and that `GroupChat.load` takes the bare group id (drop the `group:` prefix).

- [ ] **Step 4: Run tests + a real import-load of the daemon in the skenv**

Run:
```
cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skchat/tests/test_daemon_group.py ~/clawd/skcapstone-repos/skchat/tests/test_group_responder.py -q
cd ~ && ~/.skenv/bin/python -c "import skchat.daemon; print('daemon imports OK')"
```
Expected: tests PASS; daemon imports OK.

- [ ] **Step 5: Commit**

```bash
cd ~/clawd/skcapstone-repos/skchat
git add src/skchat/daemon.py tests/test_daemon_group.py
git commit -m "feat(group): daemon routes group @mentions through GroupResponder"
```

---

### Task 7: Provision the group + wire both agents (runbook + config)

**Files:**
- Create: `docs/runbooks/group-chat-setup.md`
- Modify: `~/.config/systemd/user/skchat-daemon.service.d/group.conf` (drop-in), `~/.config/systemd/user/skchat-daemon-opus.service.d/group.conf`

- [ ] **Step 1: Create the group with all three members**

```bash
cd ~
GID=$(~/.skenv/bin/skchat group create "Chef+Lumina+Opus" -d "SKOS room" | grep -oE '[0-9a-f-]{36}' | head -1)
echo "group id: $GID"
~/.skenv/bin/skchat group add-member "$GID" lumina
~/.skenv/bin/skchat group add-member "$GID" opus
~/.skenv/bin/skchat group members "$GID"
```

- [ ] **Step 2: Config drop-ins so each daemon serves the group (dev backend first)**

```bash
for svc in skchat-daemon skchat-daemon-opus; do
  d=~/.config/systemd/user/$svc.service.d; mkdir -p "$d"
  cat > "$d/group.conf" <<EOF
[Service]
Environment=SKCHAT_GROUPS=group:$GID
# DEV backend first (a live skgateway model); flip to reg:ornith once ornith lands.
Environment=SKCHAT_GROUP_BACKEND_URL=http://localhost:18780/v1/chat/completions
Environment=SKCHAT_GROUP_MODEL=qwen/qwen3.5-122b-a10b
EOF
done
systemctl --user daemon-reload
systemctl --user restart skchat-daemon skchat-daemon-opus
```

- [ ] **Step 3: Write the runbook** documenting the above + the `reg:ornith` flip (set `SKCHAT_GROUP_MODEL=reg:ornith`, `daemon-reload`, restart) + how to verify (`journalctl --user -u skchat-daemon -f`).

- [ ] **Step 4: Verify both daemons loaded the group responder**

Run:
```
journalctl --user -u skchat-daemon --since "30 seconds ago" | grep -iE "group|error"
tr '\0' '\n' < /proc/$(systemctl --user show skchat-daemon -p MainPID --value)/environ | grep SKCHAT_GROUP
```
Expected: `SKCHAT_GROUPS`/model present; no errors.

- [ ] **Step 5: Commit**

```bash
cd ~/clawd/skcapstone-repos/skchat
git add docs/runbooks/group-chat-setup.md
git commit -m "docs(group): setup runbook (provision group + per-agent daemon config)"
```

---

### Task 8: Live smoke — Chef in the Flutter app, both agents reply

**Files:** none (verification)

- [ ] **Step 1: Send a test message as Chef** (until the app is confirmed, drive via CLI as a stand-in sender that IS a member):

```bash
# from a member identity (e.g. chef) -> the group; confirm each agent answers only its mention
~/.skenv/bin/skchat group send "$GID" "@lumina @opus say hi in one line each"
sleep 30
~/.skenv/bin/skchat group send "$GID" "@opus only you: what's 2+2?"
```

- [ ] **Step 2: Verify** in `journalctl --user -u skchat-daemon -f` and `-u skchat-daemon-opus -f`: lumina+opus both reply to the `@all`-style line; only opus replies to the `@opus`-only line; each reply lands in the group (`~/.skenv/bin/skchat group info "$GID"` / the group history).

- [ ] **Step 3: Open the skchat Flutter app**, open the group, confirm multi-agent rendering (per-sender labels, live updates). File any rendering polish as follow-up items (spec Open Questions).

- [ ] **Step 4: Flip to ornith** — set `SKCHAT_GROUP_MODEL=reg:ornith` in both drop-ins, `daemon-reload`, restart, repeat one `@all` message, confirm replies still land.

- [ ] **Step 5: Capture the win to the SKOS unified GTD** (per CLAUDE.md convention) — e.g. `~/.skenv/bin/skcapstone gtd capture "SKChat group chat MVP live (Chef+Lumina+Opus)" --source skchat` — and note P2 (tool-loop) as the next item.

---

## Self-Review

- **Spec coverage:** group provisioning (T7), GroupResponder core (T1–T5), daemon integration (T6), Flutter verify (T8), backend config-knob + reg:ornith flip (T7/T8), error handling (T3/T4 soft-fail; on_error config), loop prevention (T2 self-guard + mention-only), testing (unit T1–T5, integration T6, live T8). All covered.
- **Placeholders:** none — every code step is complete. The one "verify against real daemon.py" note in T6 is a real integration check, with the exact names/line ranges to confirm.
- **Type consistency:** `GroupResponderConfig` fields, `should_respond(content, sender, cfg)`, `generate(messages, cfg, http)`, `recall(query, store, limit)`, `store_turn(user_text, reply, gid, store)`, `GroupResponder.respond(msg)` are consistent across tasks and tests.
