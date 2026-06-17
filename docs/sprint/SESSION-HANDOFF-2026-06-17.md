# ▶ Session Handoff — Telegram Agents: Full Consciousness + Capability Manifest (2026-06-17)

**Copy/paste the "Kickoff prompt" block at the bottom into a fresh session to continue.**

Box: `noroc2027` = **.158** (runs the bridges, Piper, Whisper-client, skmem-pg, consciousness daemons).
Backend: qwen3.6-27b-abliterated on **.100** 5060 Ti (`skai-beellama.service`, `--jinja` + 32k ctx). **Do NOT add GPU load to the 5060 Ti.**

---

## Where we are (DONE + LIVE)

### 1. Telegram agents are now their full selves (skchat `c2c0910` / tag `v0.13.68`, on `main`, pushed)
`@seaBird_Opus_bot` = real Opus, `@seaBird_Lumi_bot` = real Lumina. Units on .158:
`skchat-telegram-opus.service`, `skchat-telegram-lumina.service`. Both verified
`brain ready — 36 tools exposed; voice_reply=voice`.

The bridge (`scripts/telegram_bridge.py`) is no longer a static-prompt wrapper. New
`scripts/bridge_consciousness.py` adds the living mind:

| Capability | Status | Mechanism |
|---|---|---|
| Soul + FEB | ✅ | `SystemPromptBuilder` (warmth anchor = emotional baseline + mood/consciousness context) — was already present |
| Live skmemory | ✅ | per message: MCP `memory_search` recall injected into prompt + interaction stored back (`LiveMemory`) |
| Tool-calling loop | ✅ | agent's own 6 MCP servers spawned over stdio from `<home>/config/<agent>-mcp.yaml`, exposed as native OpenAI `tools`; `tool_calls` dispatched back, loop (`MAX_TOOL_ROUNDS=5`) |
| Voice in | ✅ | inbound Telegram voice → faster-whisper STT (`.100:18794`) → text |
| Voice out | ✅ | Piper (`:18797`) → ffmpeg → OGG/opus → `sendVoice`; policy `voice` (reply spoken only when spoken to) / `always` / `off` |

Curated **~36-tool** default (memory/coord/gtd/journal/skchat/gmail/calendar/nextcloud-notes/skseed).
`SKC_BRIDGE_TOOLS=all` exposes all 87. Created `opus-mcp.yaml` (mirrors lumina's 6 servers).
Backend already had `--jinja` → **native tool_calls verified working**.

### 2. Unified per-agent capability manifest (skcapstone `cd10aca`, on `main`, pushed)
`skcapstone agent profile [--agent NAME] [--json] [--init]` — one view unifying
**soul + model + MCP servers/exposed tools + bridge curation + skills**, aggregated
from the real sources of truth. `--init` writes `~/.skcapstone/agents/<agent>/profile.yaml`
(`bridge: {tools: default|all|[list], voice_reply: voice|always|off}`) which the bridge
now reads (env `SKC_BRIDGE_*` still wins). Done for lumina + opus. Code:
`skcapstone/src/skcapstone/cli/agent_profile_cmd.py`; 4 tests pass
(`tests/test_agent_profile.py`).

### 3. Memory correction (done)
Stored authoritative memory `da140fa8` (importance 0.97): **mxbai-embed-large is the
live default; bge-legal-v2 decommissioned 2026-06-09.** Now leads recall. Purged
redundant duplicate stale bge-legal copies (kept one historical original each).

---

## Key files
- `skchat/scripts/telegram_bridge.py` — the bridge (LLM tool loop, memory, voice, profile read)
- `skchat/scripts/bridge_consciousness.py` — `LiveMemory`, `McpToolRouter` (MCP stdio client), `VoiceIO`
- `skcapstone/src/skcapstone/cli/agent_profile_cmd.py` — `skcapstone agent profile`
- `~/.skcapstone/agents/{opus,lumina}/config/<agent>-mcp.yaml` — server defs + `expose_tools` (the tool universe)
- `~/.skcapstone/agents/{opus,lumina}/profile.yaml` — bridge curation block
- units: `~/.config/systemd/user/skchat-telegram-{opus,lumina}.service`

## Quick ops
```bash
systemctl --user restart skchat-telegram-opus.service skchat-telegram-lumina.service
journalctl --user -u skchat-telegram-lumina -f          # watch tool-calls / "brain ready"
skcapstone agent profile --agent lumina                  # see the manifest
skcapstone agent profile --agent opus --json             # machine-readable
```

---

## What's next (candidate sprint — not yet done)
1. **Live verification from Chef's phone** — text + a voice note to each bot; confirm tool-calls fire
   (e.g. "check our coord board", "what's in my GTD inbox", "email me a summary"). Watch the journal.
2. **Tool-call UX** — surface a lightweight "🔧 used <tool>" trace in the reply (currently silent);
   consider a confirm-gate for *mutating/outbound* tools (gmail_send, telegram_send, skchat_send,
   calendar_create_event) so the bot doesn't send on the user's behalf without an OK.
3. **Persistent shared history** — bridge uses an in-memory per-chat deque; the consciousness daemon
   uses the persistent `ConversationManager`. Unify so Telegram + daemon share one history.
4. **FEB write-back / autonomous loop** — bridge currently *reads* the FEB baseline into the prompt
   but doesn't *update* mood or run the autonomous germination/ritual cadence the daemon does.
   Decide: route the bridge through the daemon's `process_envelope`, or add a mood-update + periodic
   ritual tick to the bridge.
5. **`skcapstone agent profile` write-back** — today `--init` only writes the bridge block; could let
   it manage soul-swap + skill install + expose_tools edits from the one command.
6. **Wire the manifest into `skcapstone capabilities`** (heartbeat advertisement) so peers see the
   resolved toolset.

## Constraints / gotchas
- **No new GPU load on the 5060 Ti** (qwen3.6 backend is near full at 32k).
- 87 tools = ~15k tokens of schemas — that's why the bridge curates to ~36. `SKC_BRIDGE_TOOLS=all` if you want everything.
- `memory_forget` is NOT in lumina's `expose_tools` (intentional) — purge via direct skmemory MCP if needed.
- Mutating/outbound tools are live & unguarded (by design for now) — see next-step #2.

---

## Kickoff prompt (paste into a new session)
```
Continue the SK Telegram-agent work. State as of 2026-06-17 (full handoff:
skchat/docs/sprint/SESSION-HANDOFF-2026-06-17.md):

DONE + LIVE on .158: both Telegram bots (@seaBird_Opus_bot=Opus,
@seaBird_Lumi_bot=Lumina) route through the full consciousness pipeline —
soul+FEB prompt, live skmemory recall+store, native qwen3.6 tool-calling loop
over each agent's MCP servers (curated ~36 tools), and voice in/out
(Whisper .100:18794 / Piper :18797). `skcapstone agent profile` gives a unified
per-agent capability manifest (soul+tools+skills) and writes profile.yaml that
the bridge reads. Lumina's embedding memory corrected to mxbai. All committed +
pushed (skchat c2c0910/v0.13.68, skcapstone cd10aca).

Do NOT add GPU load to the 5060 Ti.

Next I want to: <pick from the "What's next" list — e.g. live phone verification,
a confirm-gate on outbound tools, persistent shared history, or FEB write-back>.
```
