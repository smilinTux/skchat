# Unified Agent Reply-Model Selection (switch models on every surface)

**Status:** design approved (brainstorm 2026-07-25), pending implementation plan.
**Author:** Lumina (with Chef). **Coord:** new card under the skchat model epic.

## 1. Goal

Let the operator switch which model an agent (Lumina / Opus) uses for its
replies from **any** chat surface, and have that surface's replies actually use
the picked model. One selection, honored everywhere:

- **App** (Flutter web + native): already has a picker; already honors the
  selection via the daemon reply path. Needs the dynamic catalog + verify the
  `isAgent` gate.
- **Telegram** (`@seaBird_Lumi_bot` / `@seaBird_Opus_bot`): has a `/model`
  command today, but on a **different** store (see §2). Unify it.
- **Voice / webui** (`lumina-call.py`, the calling conversational answerer, the
  `:8765` voice page): does not honor the selection today. Wire it.

The picker offers **both** (Chef's requirement):
- **Roles** (`sk-default`, `sk-creative`, `sk-auto`, ...) for regular use, each
  mapping to a concrete model/backend via the skos.models registry (including
  the `sk-auto` difficulty classifier).
- **Concrete models** (Claude Opus 4.8, Qwen 3.6, Ornith, ...) for power users.

The concrete-model list is fetched **live from SKGateway** (`/v1/models`) so it
is always current and validated: when SKGateway's served models change, the
picker changes with them, and a stale id can never be offered.

## 2. The problem: two parallel model stores

Today two independent stores decide an agent's model, which is why switching in
the app has no effect on Telegram:

| Store | Keyed by | Values | Used by |
|-------|----------|--------|---------|
| `agent_model.py` (`~/.skchat/agent_model.json`) | per-**agent** | concrete model ids (`claude-opus-4-8`, `qwen3.6-27b-abliterated`) | app `ModelPickerButton`; daemon `/api/v1/agent/model`; `lumina-bridge.py`/`opus-bridge.py` reply path (already routes via SKGateway with a qwen3.6 fallback) |
| `skos.models` registry (Syncthing-synced) | per-**chat** context (`chat:<id>`) + roles | logical roles (`sk-creative`, `sk-auto`, ...) mapping to backends | Telegram `/model` command + `_resolve_backend_for_chat` |

This violates the "one store, never a parallel store" principle. The design
unifies them behind a single per-agent **selection** and a single **resolver**,
while keeping the skos.models registry as the role-to-backend routing engine
(it is the more capable router: per-chat pins, `sk-auto` classifier, live YAML
reload) and keeping SKGateway as the single inference gateway.

## 2b. Backend reality (2026-07-25, from `skgateway.yaml`)

The refactor also corrects stale model naming. Authoritative SKGateway routing:

- **`.100:8082` (beellama)** — canonical id **`ornith-tiny`** (9B NVFP4+MTP, fast).
  `ornith-1.0-9b` and the **legacy alias `qwen3.6-27b-abliterated`** route to the
  SAME backend. "qwen3.6" no longer exists as a distinct model; it is ornith on
  .100. **`ornith-tiny` is the default for speed.**
- **`chiap08:11436` (beellama)** — `ornith-1.0-35b` / alias `ornith-big`
  (Ornith-1.0-35B, 256K ctx, the larger reasoning model; the "37b").
- Claude family via the anthropic backend; other cloud models via NVIDIA.

Consequences baked into this design:
- The per-agent **default selection** and the hard-fallback become **`ornith-tiny`**,
  replacing every `qwen3.6-27b-abliterated` default/fallback constant
  (`SKCHAT_LLM_FALLBACK_MODEL`, `agent_model.AVAILABLE_MODELS` seed, the
  `lumina-bridge`/telegram fallbacks).
- Because the catalog is fetched live from SKGateway (§3.2), the model list
  self-corrects; the picker collapses known **legacy aliases** (`qwen3.6...` ->
  `ornith-tiny`) to their canonical id so one real backend shows once.

## 3. Architecture

Three small units with clear boundaries; every surface depends only on the
resolver.

### 3.1 Selection store (unified, per-agent)

Extend `agent_model.py`. A per-agent **selection** is either a **role** (a name
in the skos.models registry) or a **concrete model id** (a name SKGateway
serves). Persisted at `~/.skchat/agent_model.json` (unchanged path).

- `get_selection(agent) -> str` (a role or a model id; falls back to the default
  `ornith-tiny`, the fast .100 backend).
- `set_selection(agent, value) -> str` (validates `value` is a known role OR a
  currently-served model id; raises on anything else).
- `default_selection() -> "ornith-tiny"` (was `claude-opus-4-8`; changed for
  speed per Chef).
- Back-compat: the existing `get_model`/`set_model` names remain as thin aliases
  so nothing breaks mid-migration.

A stored value is classified at resolve time: if it is in the skos role set it
is a role; else if SKGateway serves it, a model id; else stale (see §3.3).

### 3.2 Model catalog (dynamic, validated)

Replace `agent_model.AVAILABLE_MODELS` (hardcoded) with a live catalog:

- `list_choices() -> {"roles": [...], "models": [...]}`.
- `roles`: from `skos.models` registry (`sk-default`, `sk-creative`, `sk-auto`,
  `sk-vision`, `sk-code`, `sk-heavy`, `sk-synth`, `sk-embed`, plus any pinned
  concrete-role like `ornith-tiny`), each with its mapped model for display.
- `models`: from SKGateway `GET /v1/models` (currently 18: the Claude family,
  qwen3.5/3.6, ornith-*, deepseek, mistral, ...), fetched with a short-TTL cache
  (~60s) so it tracks SKGateway live without hammering it. Fail-soft: if
  SKGateway is unreachable, fall back to the last cache or a minimal static set,
  and mark the catalog `stale: true` for the UI.

### 3.3 Reply resolver (shared, the one place every surface calls)

New `reply_model.py` (or an extension of `agent_model.py`):

```
resolve_reply_backend(agent, chat_context=None) -> (base_url, model)
```

Precedence:
1. **Per-chat override** (Telegram only): an explicit `skos.models` pin on
   `chat:<id>` wins (power-user, unchanged behavior).
2. **Per-agent selection** (`get_selection(agent)`):
   - a **role** -> `skos.models.resolve(role=...)` -> `(url, model)`; `sk-auto`
     routes THROUGH SKGateway's classifier (as today).
   - a **concrete model id** -> `(SKGATEWAY_URL, id)` direct.
3. **Default** -> `ornith-tiny` (the fast .100 backend). `get_selection` returns
   this when the agent has no stored selection, so step 2 always yields a value
   and this is the floor. A per-bot default role (`SKC_BRIDGE_DEFAULT_ROLE`, e.g.
   Lumina = `sk-creative`) is honored only when it is the agent's stored
   selection, not as an implicit override of the fast default.

If the selection is a concrete id SKGateway no longer serves (validated against
the live catalog), the resolver skips it and falls through to the default
(`ornith-tiny`), logging the drop. The hard-fallback on a failed call becomes
`ornith-tiny` (the fast .100 backend, replacing the old `qwen3.6` constant),
so a reply never degrades to an echo.

This function is the extraction of the pattern `lumina-bridge.py:_skgateway_reply`
already implements; the app path is refactored onto it, and Telegram + voice
adopt it.

### 3.4 HTTP API (extend the existing endpoint)

`/api/v1/agent/model` on the daemon (`:9385`):
- `GET ?agent=<a>` -> `{agent, selection, kind: "role"|"model", resolved_model,
  catalog: {roles, models}, stale}`.
- `POST {agent, selection}` -> validates + `set_selection`; returns the new state.

Backwards compatible with the app's current `{model, available}` shape (keep
those keys populated alongside the new ones during migration).

## 4. Per-surface work

1. **App** (`skchat-app`): `ModelPickerButton` bottom sheet renders two sections
   from the dynamic catalog: **Roles** and **Models** (current marked). It
   already POSTs to `/api/v1/agent/model`; point it at the new shape. Verify the
   `conversation.isAgent` gate so the brain icon reliably shows for agent chats
   (the reported "I can't find it"); fix the flag if it is not being set. Reply
   path already honors the selection.

2. **Telegram** (`scripts/telegram_bridge.py`): the reply path
   (`_resolve_backend_for_chat`) becomes a thin call into the shared resolver
   (per-chat pin > per-agent selection > default). Extend `_handle_model_command`
   so `/model` lists **roles and models** (inline keyboard, current marked) and a
   tap either pins the chat (existing) or sets the per-agent selection. `/model`
   with no args shows both sections + the currently resolved model.

3. **Voice / webui**: `lumina-call.py`'s LLM step and the calling conversational
   answerer (Option A) resolve their model via the shared resolver
   (`resolve_reply_backend(agent)`), replacing the fixed `SKCHAT_LLM_URL`/
   `SKCHAT_LLM_MODEL`. Add a small model selector to the `:8765` voice page wired
   to the same `/api/v1/agent/model` endpoint. Effect: the model you pick also
   drives your **voice calls** with Lumina.

## 5. Testing / acceptance

- **Unit:** the resolver's precedence (per-chat pin > selection > default), role
  vs concrete-id classification, and stale-id fallthrough are pure and tested
  with a fake catalog + fake skos resolve (no live HTTP).
- **Catalog:** `list_choices()` reflects SKGateway `/v1/models`; a
  monkeypatched SKGateway with a model added/removed changes the catalog; an
  unreachable SKGateway yields `stale: true` and the cached/last set.
- **Cross-surface (live):** set a model via the app -> Telegram's next reply logs
  the same resolved model (one store); set a role via Telegram `/model` -> the
  app shows it selected. Pick a concrete model -> a voice call uses it.
- **No regression:** existing per-chat `/model` role pins still win where set;
  the hard-fallback (now `ornith-tiny`) still catches a backend error; a fresh
  agent with no selection defaults to `ornith-tiny` (fast).

## 5b. Legacy `qwen3.6` reference migration (relabel to reality)

`qwen3.6-27b-abliterated` is a stale label: it aliases ornith on `.100:8082`,
and the bridges already hit that backend name-agnostically, so they run ornith
today. This refactor relabels every live chat-reply reference to its real intent.

**Two knobs (so the eventual opus -> ornith-big cutover is one edit, per Chef):**
- `LOCAL_MODEL` = **`ornith-tiny`** (fast .100 backend; the uncensored default +
  the reliability fallback for every surface).
- `CAPABLE_MODEL` = **`claude-opus-4-8`** now, a single centralized constant/env
  Chef will later flip to **`ornith-big`** (the chiap08 35B) with no code sweep.
  Uncensored jobs must NOT use `CAPABLE_MODEL` while it is a Claude model (Opus
  refuses the unhinged/creative content); they stay on `LOCAL_MODEL`.

**skchat active references -> `ornith-tiny`** (all uncensored / fallback /
utility; a pure truth-in-labeling change, no behavior change since the backend is
already ornith):
- `scripts/telegram_bridge.py:94` `SKC_BRIDGE_LLM_MODEL` default
- `scripts/bridge_consciousness.py:487` `DEFAULT_LLM_MODEL`
- `scripts/lumina-bridge.py:543` + `scripts/opus-bridge.py:474`
  `SKCHAT_LLM_FALLBACK_MODEL`
- `src/skchat/voice_engine/config.py:46` `SKVOICE_FALLBACK_MODEL`
- `src/skchat/agent_model.py:48` hardcoded entry (removed; the catalog is dynamic)
- systemd env (live): `skchat-telegram-opus.service` + `skchat-telegram-lumina.service`
  `SKC_BRIDGE_LLM_MODEL=` (relabel + `daemon-reload`/restart the two bridges)
- `~/.skcapstone/agents/lumina/config/skwhisper.toml:20` `summarize_model`

**Related follow-on (tracked, NOT in this plan):** the *capable/reasoning* jobs
outside skchat that used qwen3.6 for its abliterated reasoning -- skingest
wiki-LLM synthesis (`SKINGEST_WIKI_LLM_MODEL`), `sktrip`, the wiki `relens.py`
dual-lens -- move to `CAPABLE_MODEL` (opus now). These get their own categorized
sweep so each is retargeted by intent, not blindly. The `qwen3.6-27b-abliterated`
SKGateway alias is retained until all references are gone, then retired.

**Explicitly out of scope:** the `qwen3.6-VL` vision/OCR references in skingest
are a different model and pipeline (image OCR to `.100`/`chiap08` vision
endpoints), not a chat-reply model; untouched here.

## 6. Out of scope

- Native `.41` operator-token persistence (that is the separate calling Option A
  A2 follow-on).
- Changing skos.models' role definitions or the `sk-auto` classifier itself.
- Per-message model selection (this is per-agent + per-chat only).

## 7. Component boundaries (isolation check)

- **`agent_model` (store):** what does it do — persist a per-agent selection;
  how you use it — `get_selection`/`set_selection`; depends on — nothing (a JSON
  file).
- **catalog:** what — the validated list of roles+models; use — `list_choices`;
  depends on — skos.models registry + SKGateway `/v1/models`.
- **`reply_model.resolve_reply_backend` (resolver):** what — turn (agent, chat)
  into (url, model); use — one call; depends on — store + skos.models + catalog.
- **surfaces (app / Telegram / voice):** each depends ONLY on the resolver (for
  replies) and the HTTP API (for the picker). None reaches into the store or
  skos.models directly for reply routing anymore.
