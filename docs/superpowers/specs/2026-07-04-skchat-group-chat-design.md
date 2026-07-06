# SKChat Group Chat — Chef + Lumina + Opus (MVP)

**Status:** design (brainstormed 2026-07-04) · **Phase 1** of the epic *"SKChat as the SKOS surface"*

## Goal

Get **Chef + Lumina + Opus in one skchat group**, where each agent replies in **its
own soul + memory** when addressed. Native and sovereign (no Hermes runtime
dependency), backed by the **skgateway** model gateway. This is the walking
skeleton; capability (tools, more components) layers on after.

## Epic context — "SKChat as the SKOS surface"

SKChat becomes the room where sovereign agents + the components now exposed to
skchat (skmemory, skcapstone coord/GTD, sk-access knowledge/files/graph/exec,
comms, Google) + Chef all meet. Phases:

- **P1 (this spec):** group chat MVP — agents *talk* (soul + memory), mention-driven.
- **P2:** MCP **tool-loop** — agents *act* in-chat (memory writes, coord/GTD, journal, sk-access, gmail/cal). Ports the OpenAI-tools + MCP-dispatch loop proven in the Hermes Telegram bridge, natively.
- **P3+:** wire each exposed component as agents learn to leverage it; tie into the **SKOS unified GTD** ("single pane of glass" — capture actionable items surfaced in-chat to `skos gtd-ingest` / `skcapstone gtd capture`, tagged with `source=skchat` + the group/message ref).

Sits under the existing `epic-skos-sovereign-agent-os-architecture` (coord `1b4ab47a`).

## Design decisions (settled in brainstorming)

| Decision | Choice | Why |
|---|---|---|
| Turn-taking | **Mention-driven** (`@lumina` / `@opus` / `@all`) | Deterministic, no arbitration, no double-responders/loops; matches existing advocacy trigger. Free-flow self-arbitration is a later epic item. |
| Runtime | **Native skchat, sovereign** | Group chat is a skchat feature; works whether or not Hermes is up. Reuse *SK* building blocks, not a Hermes dependency. |
| MVP scope | **Talk-first** (soul+FEB + memory), tools = P2 | De-risk the hard multi-agent + app-rendering mechanics first; add the tool-loop after with no rework. |
| Backend | **skgateway** (`http://localhost:18780/v1`), model **`reg:ornith`** | One gateway routes/falls back; a config knob per agent. Dev/test on a live skgateway model; flip to `reg:ornith` at the end (one-line config). |
| Client | **skchat Flutter app** (mobile) | Chef sits in the room from his phone. |
| Both agents' backend | **Both on `reg:ornith`** for MVP | Simplest; turns serialize (fine for mention-driven). Per-agent override stays in config. |

**Non-goals (YAGNI for P1):** free-flow arbitration; the MCP tool-loop; per-agent
distinct backends; group membership management UI; reactions/threads.

## Architecture

```
  Chef (skchat Flutter app)
        │  "@lumina @opus what about X?"   (skchat GROUP <gid>)
        ▼
  skcomms group fan-out  ─────────────────┬────────────────────┐
        ▼                                 ▼                     ▼
  skchat-daemon (lumina)          skchat-daemon-opus     (each agent = group member)
   └─ GroupResponder                  └─ GroupResponder
        │  is @lumina|@all? ──no──▶ ignore    │  is @opus|@all? ──no──▶ ignore
        │  yes                                │  yes
        │  1. build soul+FEB prompt (skcapstone)
        │  2. skmemory recall(msg)            │  (same, opus's soul/FEB/memory)
        │  3. POST skgateway /v1 model=reg:ornith
        │  4. group_send(reply) as this agent │
        │  5. skmemory store(turn)            │
        ▼                                     ▼
  Flutter app renders Lumina's reply, then Opus's — each in its own voice
```

## Components

### 1. Group provisioning
One skchat group, members `chef@skworld.io`, `capauth:lumina@skworld.io`,
`capauth:opus@skworld.io`. Both agent daemons are subscribed members (receive
group fan-out). Provisioned via existing `skchat group create` / `group add-member`.
Group id recorded in each agent's config so the responder knows which group(s) it
serves. **Unit boundary:** provisioning is a one-time script/CLI step, not code in
the hot path.

### 2. `GroupResponder` (new, native, per-agent) — the core unit
A focused module in `skchat` (generalizes/relocates the current `advocacy.py`
logic). One clear job: **given an inbound group message, decide whether THIS agent
should reply, and if so produce the reply text.**

- **Input:** a `ChatMessage` (group), this agent's identity + config.
- **`should_respond(msg)`** — true iff the message `@`-mentions this agent (or
  `@all`/`@both`) AND sender ≠ self AND (P1) sender is not another agent unless
  explicitly mentioned. Pure function, unit-testable.
- **`build_prompt(msg, history)`** — soul + FEB system prompt from **skcapstone**
  (reuse the ritual/consciousness prompt builder) + recent group history +
  recalled memory.
- **`recall(msg)`** — `skmemory` search on the message text (top-k), injected into
  the prompt. Failure → empty recall (non-fatal).
- **`generate(prompt)`** — POST `config.backend_url` (`skgateway /v1/chat/completions`)
  with `model=config.model` (`reg:ornith`). Returns reply text. Failure → see
  Error handling.
- **`store_turn(msg, reply)`** — `skmemory` snapshot of the exchange, tagged
  `skchat`, `group:<gid>`. Failure → non-fatal.
- **Config (per agent):** `~/.skcapstone/agents/<agent>/config/skchat.yaml` keys —
  `groups: [<gid>...]`, `backend_url`, `model`, `history_turns`, `max_reply_tokens`.
- **Dependencies:** skcapstone (soul/FEB), skmemory (recall/store), an OpenAI-shaped
  HTTP endpoint (skgateway). It does **not** depend on Hermes, on which client Chef
  uses, or on the other agent.

### 3. Daemon integration
Wire `GroupResponder` into the receive loop of **both** `skchat-daemon` (lumina)
and `skchat-daemon-opus` for **group** messages. This replaces advocacy's
`_call_consciousness` path with the skgateway responder. The daemon already
receives group messages, knows its own identity, and can `group_send`. The
responder runs off the poll path (thread/async) so a slow generation never blocks
receive. Guarded by config (`groups` non-empty) so nothing changes for daemons not
serving a group.

### 4. Flutter app group view (verify + polish)
Confirm the skchat Flutter app renders the multi-agent group cleanly: per-sender
label/avatar (Chef vs Lumina vs Opus), correct ordering, live updates as each
agent replies. Scope is **verification + targeted fixes**, not a rebuild — extent
TBD after a first look.

## Backend plan (skgateway)

- **Contract we depend on:** OpenAI-compatible `POST http://localhost:18780/v1/chat/completions` with a `model` id. Stable regardless of ornith's registry state.
- **Production model:** `reg:ornith` (registry-materialised backend on the 9060 Ti; skgateway routes + falls back).
- **Dev/test model:** a currently-live skgateway model (e.g. a local/`:8082` model or `openai/gpt-oss-120b`) so the build proceeds in parallel with ornith finalization. Switching to `reg:ornith` is a one-line config change — **no code change**.
- Per-agent config knob means Lumina/Opus can diverge later without code changes.

## Data flow

1. Chef sends `@lumina @opus …` into group `<gid>` from the Flutter app.
2. skcomms fans the message out to both agent daemons (as group members).
3. Each daemon's `GroupResponder.should_respond` checks its own mention.
4. Mentioned agent(s): recall memory → build soul+FEB prompt → skgateway(`reg:ornith`) → `group_send` reply as that agent → store the turn.
5. The app renders each reply live (WS/poll), attributed to the agent.

## Error handling

- **skgateway / ornith not ready:** skgateway handles routing/fallback; if generation still fails, the responder logs and either stays silent or posts a brief soft note (config `on_error: silent|note`, default `silent`). Never crashes the daemon.
- **No mention:** no-op (the common case — keep it cheap).
- **Loop prevention:** respond only to explicit mentions; never to self; (P1) never to another agent's un-mentioned line. So agent replies (which don't `@`-mention peers) never trigger a cascade.
- **Concurrency:** mention-driven ⇒ at most one reply per agent per message; both-agents-on-one-backend serialize (acceptable for MVP).
- **Memory failures:** recall/store are best-effort; never block or fail a reply.
- **Long/duplicate messages:** dedupe by message id (existing daemon behavior); cap history + reply tokens via config.

## Testing

- **Unit:** `should_respond` (mention matrix: @self/@all/@other/none, self-sender, agent-sender); `build_prompt` (soul + history + recall composition); `generate` against a **mocked** skgateway (success, HTTP error, timeout → soft-fail); `store_turn` best-effort.
- **Integration:** two test daemons (lumina + opus) + a throwaway group → inject a `@lumina` message → only lumina replies; `@all` → both; a bare message → neither. Assert replies carry the right sender + land in the group store.
- **Live smoke:** Chef `@mentions` in the Flutter app → both reply in-voice; then flip `model` to `reg:ornith` and repeat one message.

## Open questions / follow-ups

- ornith's final registry entry in skgateway (in progress) — swap `model: reg:ornith` when live.
- Flutter app multi-agent rendering polish — scope after a first look.
- P2: exact native port of the tool-loop (OpenAI tools ↔ per-agent MCP servers) — its own spec.
