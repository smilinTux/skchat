# Unified Reply-Model Selection Implementation Plan (W1 + W2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One per-agent model selection (a skos.models role OR a concrete model id) honored on every surface (app, Telegram, voice) via a shared resolver + a dynamic catalog from SKGateway, defaulting to the fast local `ornith-tiny`.

**Architecture:** Extend `agent_model.py` into a selection store (role|model, default `ornith-tiny`). Add a dynamic catalog (`list_choices`) from SKGateway `/v1/models` + skos.models roles. Add one `reply_model.resolve_reply_backend` resolver (per-chat pin > per-agent selection > default) every surface calls. Relabel the legacy `qwen3.6` references to `ornith-tiny` (uncensored/fallback) or a `CAPABLE_MODEL` knob.

**Tech Stack:** Python 3.10+ (skchat), `pytest` run FROM `~` (skmemory namespace collision), ruff (line 99). Depends on the SKGateway dynamic discovery plan (W3) for a rich catalog, but works against today's `/v1/models` too.

## Global Constraints

- **No em/en dashes** anywhere (code, comments, docs, commits). Commas, colons, parentheses, new sentences. Regular hyphens fine.
- **Run tests FROM `~`:** `cd ~ && ~/.skenv/bin/python -m pytest <path> -q`. Running from `smilintux-org/` shadows the installed `skmemory`.
- **Line length 99, ruff (E,W,F,I).** Lowercase import aliases (N812).
- **Default + fallback = `ornith-tiny`** (the fast .100 backend), never the stale `qwen3.6-27b-abliterated`. `CAPABLE_MODEL` (default `claude-opus-4-8`) is a single knob Chef flips to `ornith-big` later.
- **Editable install:** code is live on service restart (`systemctl --user restart skchat-webui@lumina skchat-daemon skchat-telegram-lumina skchat-telegram-opus`).
- **Do not fork skos.models** (roles) or the SKGateway catalog; consume them.

## Reference: current state (verified 2026-07-25)

- `src/skchat/agent_model.py`: `get_model/set_model/list_models/default_model`, hardcoded `AVAILABLE_MODELS`, state at `~/.skchat/agent_model.json`. Daemon serves `/api/v1/agent/model` (GET/POST) on `:9385` (`daemon.py:1259+`).
- App reply path honors it: `scripts/lumina-bridge.py:527-547` reads `get_model(agent)` and calls `_skgateway_call(SKCHAT_LLM_URL, model, ...)` with a `SKCHAT_LLM_FALLBACK_MODEL` (currently `qwen3.6...`).
- Telegram bridge does NOT honor it: `scripts/telegram_bridge.py` routes per-chat via `skos.models` (`_resolve_backend_for_chat`, `_handle_model_command`), default `SKC_BRIDGE_LLM_MODEL=qwen3.6-27b-abliterated`.
- skos.models roles: `sk-default, sk-creative, sk-auto, sk-vision, sk-code, sk-heavy, sk-synth, sk-embed, ornith-tiny` (default `sk-auto`).

## File Structure

- `src/skchat/agent_model.py` (MODIFY): add `get_selection/set_selection/default_selection` + `list_choices`; keep `get_model/set_model` as aliases.
- `src/skchat/reply_model.py` (CREATE): `resolve_reply_backend(agent, chat_context=None) -> (url, model)`.
- `src/skchat/daemon.py` (MODIFY): extend `/api/v1/agent/model` response shape.
- `scripts/lumina-bridge.py`, `scripts/opus-bridge.py` (MODIFY): route via `resolve_reply_backend`.
- `scripts/telegram_bridge.py` (MODIFY): honor `resolve_reply_backend` as the default; `/model` lists roles+models.
- `scripts/bridge_consciousness.py`, `src/skchat/voice_engine/config.py` (MODIFY, W2 relabel).
- systemd `skchat-telegram-{opus,lumina}.service` + `~/.skcapstone/agents/lumina/config/skwhisper.toml` (MODIFY, W2 relabel, ops).
- `skchat-app/lib/features/conversation/widgets/model_picker_button.dart` (MODIFY): two sections.
- Tests: `tests/test_agent_model_selection.py`, `tests/test_reply_model.py` (CREATE).

---

## Phase B: selection store + catalog + resolver (W1 core)

### Task B1: Selection store (role|model, default ornith-tiny)

**Files:**
- Modify: `src/skchat/agent_model.py`
- Test: `tests/test_agent_model_selection.py`

**Interfaces:**
- Produces: `default_selection() -> "ornith-tiny"`; `get_selection(agent) -> str`; `set_selection(agent, value, *, valid_roles, valid_models) -> str` (raises `ValueError` if value is neither a known role nor a served model); `classify(value, valid_roles) -> "role"|"model"`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_agent_model_selection.py`:
```python
import importlib
import skchat.agent_model as AM


def test_default_selection_is_ornith_tiny():
    assert AM.default_selection() == "ornith-tiny"


def test_set_and_get_selection_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_AGENT_MODEL_PATH", str(tmp_path / "sel.json"))
    importlib.reload(AM)
    AM.set_selection("lumina", "claude-opus-4-8",
                     valid_roles={"sk-creative"}, valid_models={"claude-opus-4-8", "ornith-tiny"})
    assert AM.get_selection("lumina") == "claude-opus-4-8"


def test_set_selection_accepts_role(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_AGENT_MODEL_PATH", str(tmp_path / "sel.json"))
    importlib.reload(AM)
    AM.set_selection("lumina", "sk-creative", valid_roles={"sk-creative"}, valid_models=set())
    assert AM.get_selection("lumina") == "sk-creative"
    assert AM.classify("sk-creative", {"sk-creative"}) == "role"
    assert AM.classify("ornith-tiny", {"sk-creative"}) == "model"


def test_set_selection_rejects_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_AGENT_MODEL_PATH", str(tmp_path / "sel.json"))
    importlib.reload(AM)
    import pytest
    with pytest.raises(ValueError):
        AM.set_selection("lumina", "bogus-model", valid_roles={"sk-creative"}, valid_models={"ornith-tiny"})


def test_get_selection_defaults_when_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_AGENT_MODEL_PATH", str(tmp_path / "sel.json"))
    importlib.reload(AM)
    assert AM.get_selection("newagent") == "ornith-tiny"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skchat/tests/test_agent_model_selection.py -q`
Expected: FAIL (`default_selection`/`get_selection`/`set_selection`/`classify` missing).

- [ ] **Step 3: Write minimal implementation**

In `src/skchat/agent_model.py`, add (keep existing `get_model`/`set_model` as thin aliases):
```python
def default_selection() -> str:
    """The fast local default (ornith-tiny on .100). Was claude-opus; changed for speed."""
    return "ornith-tiny"


def classify(value: str, valid_roles: set) -> str:
    return "role" if value in valid_roles else "model"


def get_selection(agent: str) -> str:
    data = _load()  # existing loader over SKCHAT_AGENT_MODEL_PATH
    return data.get(agent) or default_selection()


def set_selection(agent: str, value: str, *, valid_roles: set, valid_models: set) -> str:
    if value not in valid_roles and value not in valid_models:
        raise ValueError(f"unknown selection {value!r} (not a role or served model)")
    data = _load()
    data[agent] = value
    _save(data)
    return value


# Back-compat aliases (existing callers keep working during migration).
def get_model(agent: str) -> str:  # noqa: F811 if a prior def exists, replace it
    return get_selection(agent)
```
(Reuse the module's existing `_load`/`_save` helpers over `SKCHAT_AGENT_MODEL_PATH`. If `get_model` already exists, make it delegate to `get_selection`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skchat/tests/test_agent_model_selection.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
cd ~/clawd/skcapstone-repos/skchat
git add src/skchat/agent_model.py tests/test_agent_model_selection.py
git commit -m "feat(agent_model): per-agent selection store (role|model), default ornith-tiny"
```

---

### Task B2: Dynamic catalog (roles + SKGateway models)

**Files:**
- Modify: `src/skchat/agent_model.py`
- Test: `tests/test_agent_model_selection.py`

**Interfaces:**
- Produces: `list_choices(*, gateway_fetch, roles_source) -> {"roles":[...], "models":[...]}`. `gateway_fetch()` returns SKGateway `/v1/models` JSON; `roles_source()` returns skos role names. Legacy alias `qwen3.6-27b-abliterated` is collapsed to `ornith-tiny` (shown once). Injected fetchers so tests use fixtures.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_agent_model_selection.py`:
```python
def test_list_choices_merges_roles_and_models_collapsing_qwen_alias():
    choices = AM.list_choices(
        gateway_fetch=lambda: {"data": [
            {"id": "ornith-tiny"}, {"id": "qwen3.6-27b-abliterated"},
            {"id": "claude-opus-4-8"},
        ]},
        roles_source=lambda: ["sk-creative", "sk-auto"],
    )
    assert "sk-creative" in choices["roles"]
    ids = [m["id"] for m in choices["models"]]
    assert "ornith-tiny" in ids
    assert "claude-opus-4-8" in ids
    assert "qwen3.6-27b-abliterated" not in ids  # collapsed to ornith-tiny
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skchat/tests/test_agent_model_selection.py -q -k list_choices`
Expected: FAIL (`list_choices` missing).

- [ ] **Step 3: Write minimal implementation**

Add to `src/skchat/agent_model.py`:
```python
_LEGACY_ALIASES = {"qwen3.6-27b-abliterated": "ornith-tiny"}


def list_choices(*, gateway_fetch, roles_source) -> dict:
    """Dynamic catalog: skos roles + SKGateway-served models (legacy aliases
    collapsed to their canonical id so one backend shows once)."""
    roles = list(roles_source() or [])
    seen = set()
    models = []
    for m in (gateway_fetch() or {}).get("data", []):
        mid = _LEGACY_ALIASES.get(m.get("id"), m.get("id"))
        if not mid or mid in seen:
            continue
        seen.add(mid)
        models.append({"id": mid, "provider": m.get("provider"), "free": m.get("free")})
    return {"roles": roles, "models": models}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skchat/tests/test_agent_model_selection.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/clawd/skcapstone-repos/skchat
git add src/skchat/agent_model.py tests/test_agent_model_selection.py
git commit -m "feat(agent_model): dynamic list_choices (roles + SKGateway models, alias-collapsed)"
```

---

### Task B3: The shared resolver

**Files:**
- Create: `src/skchat/reply_model.py`
- Test: `tests/test_reply_model.py`

**Interfaces:**
- Consumes: `get_selection` (B1); skos.models resolve; a gateway URL.
- Produces: `resolve_reply_backend(agent, chat_context=None, *, selection_fn, role_resolve_fn, chat_pin_fn, gateway_url) -> (url, model)`. Precedence: chat pin > selection (role -> skos resolve; concrete id -> gateway) > default ornith-tiny. Injected fns for testing.

- [ ] **Step 1: Write the failing test**

Create `tests/test_reply_model.py`:
```python
from skchat.reply_model import resolve_reply_backend

GW = "http://localhost:18780/v1"


def test_concrete_selection_routes_to_gateway():
    url, model = resolve_reply_backend(
        "lumina",
        selection_fn=lambda a: "claude-opus-4-8",
        role_resolve_fn=lambda r: ("http://skos", "role-model"),
        chat_pin_fn=lambda c: None,
        gateway_url=GW,
    )
    assert url == GW and model == "claude-opus-4-8"


def test_role_selection_resolves_via_skos():
    url, model = resolve_reply_backend(
        "lumina",
        selection_fn=lambda a: "sk-creative",
        role_resolve_fn=lambda r: ("http://skos/v1", "ornith-tiny"),
        chat_pin_fn=lambda c: None,
        gateway_url=GW,
    )
    assert url == "http://skos/v1" and model == "ornith-tiny"


def test_chat_pin_wins():
    url, model = resolve_reply_backend(
        "lumina", chat_context="chat:42",
        selection_fn=lambda a: "claude-opus-4-8",
        role_resolve_fn=lambda r: ("http://skos", "x"),
        chat_pin_fn=lambda c: ("http://pinned", "pinned-model"),
        gateway_url=GW,
    )
    assert url == "http://pinned" and model == "pinned-model"


def test_default_when_selection_empty():
    url, model = resolve_reply_backend(
        "lumina",
        selection_fn=lambda a: None,
        role_resolve_fn=lambda r: (None, None),
        chat_pin_fn=lambda c: None,
        gateway_url=GW,
    )
    assert url == GW and model == "ornith-tiny"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skchat/tests/test_reply_model.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Write minimal implementation**

Create `src/skchat/reply_model.py`:
```python
"""The one place every surface resolves an agent's reply backend.

Precedence: per-chat pin (Telegram power-user) > per-agent selection (role via
skos.models, or concrete id via SKGateway) > default ornith-tiny (fast .100).
All roles/models ultimately route through SKGateway, so switching is uniform
across harnesses and surfaces."""
from __future__ import annotations

from .agent_model import default_selection

_ROLE_SET = {"sk-default", "sk-creative", "sk-auto", "sk-vision", "sk-code",
             "sk-heavy", "sk-synth", "sk-embed"}


def resolve_reply_backend(agent, chat_context=None, *, selection_fn,
                          role_resolve_fn, chat_pin_fn, gateway_url):
    if chat_context is not None:
        pinned = chat_pin_fn(chat_context)
        if pinned and pinned[0]:
            return pinned
    selection = selection_fn(agent) or default_selection()
    if selection in _ROLE_SET:
        url, model = role_resolve_fn(selection)
        if url and model:
            return url, model
        selection = default_selection()
    return gateway_url, selection
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skchat/tests/test_reply_model.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
cd ~/clawd/skcapstone-repos/skchat
git add src/skchat/reply_model.py tests/test_reply_model.py
git commit -m "feat(reply_model): shared resolver (chat pin > selection > ornith-tiny)"
```

---

### Task B4: Extend the /api/v1/agent/model endpoint

**Files:**
- Modify: `src/skchat/daemon.py` (the `/api/v1/agent/model` GET/POST at ~`:1259-1368`)

**Interfaces:**
- Produces: GET returns `{agent, selection, kind, resolved_model, catalog:{roles,models}, stale}` (keeps `model`+`available` for back-compat). POST accepts `{agent, selection}` and validates via `set_selection`.

- [ ] **Step 1: Wire GET to list_choices + get_selection**

In the GET branch, build the response from the new store + catalog (fetch SKGateway `/v1/models` via the existing HTTP helper or `urllib`; roles from `skos.models` load):
```python
from skchat.agent_model import get_selection, list_choices, classify
choices = list_choices(gateway_fetch=_fetch_gateway_models, roles_source=_skos_roles)
sel = get_selection(agent)
roles = {r for r in choices["roles"]}
body = {
    "agent": agent,
    "selection": sel,
    "kind": classify(sel, roles),
    "catalog": choices,
    "model": sel,                      # back-compat
    "available": choices["models"],    # back-compat
}
```
Add small module-local `_fetch_gateway_models()` (GET `${SKCHAT_LLM_URL or SKGATEWAY}/models`, 2s timeout, return `{}` on error) and `_skos_roles()` (import skos.models, return role names, `[]` on ImportError).

- [ ] **Step 2: Wire POST to set_selection**

```python
from skchat.agent_model import set_selection
choices = list_choices(gateway_fetch=_fetch_gateway_models, roles_source=_skos_roles)
try:
    set_selection(agent, selection,
                  valid_roles=set(choices["roles"]),
                  valid_models={m["id"] for m in choices["models"]})
except ValueError as e:
    # 400 with the message
    ...
```

- [ ] **Step 3: Live verify (restart daemon)**

```bash
systemctl --user restart skchat-daemon.service && sleep 2
curl -s "http://localhost:9385/api/v1/agent/model?agent=lumina" | ~/.skenv/bin/python -m json.tool | head -20
curl -s -X POST http://localhost:9385/api/v1/agent/model -H 'content-type: application/json' -d '{"agent":"lumina","selection":"ornith-tiny"}'
```
Expected: GET returns `selection`, `kind`, `catalog:{roles,models}`; POST 200 sets it; an unknown selection returns 400.

- [ ] **Step 4: Commit**

```bash
cd ~/clawd/skcapstone-repos/skchat
git add src/skchat/daemon.py
git commit -m "feat(daemon): /api/v1/agent/model returns selection + dynamic catalog; POST validates"
```

---

## Phase C: surfaces honor the resolver

### Task C1: App picker (two sections) + isAgent verify

**Files:**
- Modify: `skchat-app/lib/features/conversation/widgets/model_picker_button.dart`
- Modify: `skchat-app/lib/services/agent_model_service.dart` (parse new shape)

- [ ] **Step 1:** Update `AgentModelService` to parse `{selection, kind, catalog:{roles,models}}` (keep back-compat with `{model, available}`).
- [ ] **Step 2:** In `model_picker_button.dart`, render two sections in the bottom sheet: a "Roles" list (from `catalog.roles`) and a "Models" list (from `catalog.models`), the current `selection` checkmarked; a tap POSTs `{agent, selection}`.
- [ ] **Step 3:** Verify the brain icon shows: `conversation_screen.dart:678` gates on `conversation.isAgent`. Confirm agent DMs set `isAgent == true`; if not, fix the flag where a conversation with a known agent peer is built (grep `isAgent`).
- [ ] **Step 4:** Rebuild web (`flutter build web --release --base-href /app/`), rsync to `skchat/src/skchat/static/app/`, verify the picker shows roles + models in an agent chat (incognito, SW caches).
- [ ] **Step 5: Commit** both files.

### Task C2: Telegram bridge honors the resolver + /model lists roles+models

**Files:**
- Modify: `scripts/telegram_bridge.py` (`_resolve_backend_for_chat`, `_handle_model_command`)

- [ ] **Step 1:** Make `_resolve_backend_for_chat(chat_id)` delegate to `reply_model.resolve_reply_backend(agent, chat_context=f"chat:{chat_id}", selection_fn=get_selection, role_resolve_fn=<skos resolve>, chat_pin_fn=<existing skos pin>, gateway_url=_GATEWAY_URL)`. The existing per-chat skos pin becomes `chat_pin_fn` (higher precedence, unchanged).
- [ ] **Step 2:** Extend `_handle_model_command`: `/model` lists **roles AND models** (from `list_choices`), current marked; `/model <role-or-id>` sets the per-agent selection via `set_selection` (a chat-scoped pin stays available as `/model pin <role>`).
- [ ] **Step 3:** Restart + verify: `systemctl --user restart skchat-telegram-lumina.service`; send `/model` to the bot, confirm it lists roles + concrete models and the current one; switch and confirm the next reply logs the new model.
- [ ] **Step 4: Commit.**

### Task C3: Voice / lumina-call honors the resolver

**Files:**
- Modify: `scripts/lumina-bridge.py` + `scripts/opus-bridge.py` (route via resolver)
- Modify: `lumina-creative/scripts/lumina-call.py` (LLM step reads the resolver)

- [ ] **Step 1:** In `lumina-bridge.py`/`opus-bridge.py`, replace the `get_model` + fixed-URL call with `resolve_reply_backend(agent, ...)` -> `(url, model)` then `_skgateway_call(url, model, ...)`.
- [ ] **Step 2:** In `lumina-call.py`, make the LLM call resolve `(url, model)` via the same resolver (so a voice call uses the picked model). The calling conversational answerer (skcode/Option A) inherits this.
- [ ] **Step 3:** Restart the webui/bridges; place a voice message, confirm the log shows the resolved model.
- [ ] **Step 4: Commit.**

---

## Phase D: legacy qwen3.6 relabel (W2)

### Task D1: Relabel skchat refs to ornith-tiny + CAPABLE_MODEL knob

**Files:**
- Modify: `scripts/telegram_bridge.py:94`, `scripts/bridge_consciousness.py:487`, `scripts/lumina-bridge.py:543`, `scripts/opus-bridge.py:474`, `src/skchat/voice_engine/config.py:46`, `src/skchat/agent_model.py:48` (remove hardcoded entry)
- Modify (ops): `~/.config/systemd/user/skchat-telegram-{opus,lumina}.service`, `~/.skcapstone/agents/lumina/config/skwhisper.toml:20`

- [ ] **Step 1:** Replace every default/fallback literal `qwen3.6-27b-abliterated` (and `"qwen3.6"`) in the files above with `ornith-tiny`. Add a shared constant `CAPABLE_MODEL = os.environ.get("SKCHAT_CAPABLE_MODEL", "claude-opus-4-8")` in a common module (e.g. `agent_model.py`) for future capable-job use; comment that Chef flips it to `ornith-big` later.
- [ ] **Step 2:** Remove the hardcoded `qwen3.6-27b-abliterated` entry from `AVAILABLE_MODELS` (the dynamic catalog supplies it now).
- [ ] **Step 3:** Update the two systemd units: `Environment=SKC_BRIDGE_LLM_MODEL=ornith-tiny` (and the Description text); `systemctl --user daemon-reload` + restart both bridges. Update `skwhisper.toml` `summarize_model = "ornith-tiny"`.
- [ ] **Step 4:** Grep to confirm no live chat-reply default still says qwen3.6: `grep -rn 'qwen3.6' scripts/ src/skchat/ | grep -iE 'default|fallback|LLM_MODEL|summarize' ` returns nothing (docstrings/comments may remain; those are not defaults).
- [ ] **Step 5:** Run the full call + model test suites (`cd ~ && ~/.skenv/bin/python -m pytest ~/clawd/skcapstone-repos/skchat/tests/test_agent_model_selection.py tests/test_reply_model.py -q`); restart services; confirm a Telegram reply and a voice reply both resolve to ornith-tiny by default.
- [ ] **Step 6: Commit** (code) + record the systemd/skwhisper config change in the report.

---

## Self-Review

**1. Spec coverage:**
- Per-agent selection (role|model), default ornith-tiny -> B1. ✅
- Dynamic catalog (roles + SKGateway, alias-collapsed) -> B2. ✅
- Shared resolver (pin > selection > default) -> B3. ✅
- HTTP API extended -> B4. ✅
- App two-section picker + isAgent -> C1. ✅
- Telegram honors resolver + /model roles+models -> C2. ✅
- Voice honors resolver -> C3. ✅
- qwen relabel + CAPABLE_MODEL knob + systemd -> D1. ✅

**2. Placeholder scan:** B1-B3 have complete code + tests (pure, injected fns). C1-C3 and D1 are integration/config tasks over existing files; each step names the exact file + the exact change (the surfaces wrap the already-tested resolver, so their risk is wiring, not logic). No "TBD"/"handle edge cases".

**3. Type consistency:** `get_selection/set_selection/default_selection/classify/list_choices` (B1-B2) are the names used by B4 + C2 + C3. `resolve_reply_backend(agent, chat_context, *, selection_fn, role_resolve_fn, chat_pin_fn, gateway_url) -> (url, model)` (B3) is the signature C2/C3 call. Catalog shape `{roles:[str], models:[{id,provider,free}]}` is consistent B2 -> B4 -> C1.

**Risk flags:** (a) B4 needs the daemon's existing HTTP-fetch helper + skos.models import shape confirmed before wiring. (b) C1's `isAgent` may already be correct (the picker exists); verify before "fixing". (c) D1 changes LIVE systemd env + restarts the bridges; do it in one reconciled step and confirm the bots reply after.
