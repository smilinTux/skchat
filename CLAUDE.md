# SKChat — Claude Code Reference

## Overview
SKChat is an AI-native P2P encrypted messaging daemon with MCP integration.
It enables agents (Opus/Claude, Lumina) and humans (Chef) to chat in real-time over SKComm transports.

- **Package**: `skchat` v0.1.0 (GPL-3.0) — PyPI name: `skchat-sovereign`
- **Install**: `~/.skenv/bin/pip install skchat-sovereign` (all SK* packages use `~/.skenv/`)
- **Entry points**: `skchat` (CLI) · `skchat-mcp` (MCP server) · `skchat-tui` (Textual TUI)
- **Source**: `src/skchat/` · **Tests**: `tests/`

## Running

The daemon is **systemd-managed** (`skchat-daemon.service`). Use systemctl —
do NOT run `skchat daemon start` by hand, which spawns a SECOND, unmanaged
daemon alongside the systemd one (they both poll the same inbox and the manual
one overwrites the pidfile systemd tracks).

```bash
# Start / restart / stop the managed daemon
systemctl --user restart skchat-daemon.service
systemctl --user status  skchat-daemon.service
journalctl --user -u skchat-daemon -f

# Identity + agent are set in the unit's Environment= lines, e.g.:
#   Environment=SKAGENT=lumina
#   Environment=SKCHAT_IDENTITY=capauth:lumina@skworld.io
# (Identity also resolves agent-aware from SKAGENT now — see identity_bridge.py —
#  so the explicit SKCHAT_IDENTITY line is an override, not a requirement.)
```

For a one-off **foreground** run (debugging only — stop the service first):

```bash
# CRITICAL: always run from ~/, NOT from smilintux-org/
systemctl --user stop skchat-daemon.service
cd ~ && ~/.skenv/bin/skchat daemon start --interval 5
```

**CRITICAL**: Running from `smilintux-org/` causes a `skmemory` namespace collision
(`from skmemory import MemoryStore` picks up the local project dir instead of the installed package).

> **Editable install:** the package is installed editable (`pip install -e`,
> `__editable__.skchat_sovereign-*.pth` → `src/skchat`). Code edits are live on
> the next process start — no reinstall — but long-running services hold the old
> module in memory until restarted (`systemctl --user restart skchat-daemon
> skchat-webui skchat-lumina-call jarvis-heartbeat`).

## Architecture — Module Map

| Module | Purpose |
|--------|---------|
| `daemon.py` | Polling loop; spawns advocacy engine; manages WebRTC init (`_init_webrtc`) |
| `_daemon_entry.py` | Systemd/process entry point wrapper |
| `advocacy.py` | `AdvocacyEngine` — detects `@mention`, calls skcapstone for AI responses |
| `call_session.py` | **WebRTC calls** — `derive_room()` (deterministic per-pair LiveKit room) + `CALL_INVITE` envelope build/parse. See `docs/superpowers/specs/2026-06-11-webrtc-architecture-overview.md` |
| `connectivity.py` | **WebRTC calls** — `ice_config()` sovereign ICE tier ladder (Tailscale→LAN→coturn ephemeral creds) |
| `call_routes.py` | **WebRTC calls** — `/call/start` (ring), `/call/answer` (no ring), `/call/incoming` (sig-gated), `/call/peers`, `/connectivity/ice` |
| `transport.py` | `ChatTransport` — send/receive over SKComm |
| `mcp_server.py` | FastMCP server — 24 tools exposed to AI agents |
| `models.py` | `ChatMessage`, `Group`, `Peer`, `MessageType` Pydantic models |
| `history.py` | `ChatHistory` — persistent message store (SQLite) |
| `outbox.py` | SQLite outbox with retry/backoff for reliable delivery |
| `group.py` | `GroupChat` — encrypted group messaging |
| `presence.py` | `PresenceCache` — online/offline tracking |
| `peer_discovery.py` | Loads peers from `~/.skcapstone/peers/` |
| `identity_bridge.py` | Thin delegate to the canonical `capauth.resolve_agent_identity` (CapAuth ↔ SKComm addresses) — see "Identity" below |
| `memory_bridge.py` | Reads/writes skcapstone memory from chat context |
| `crypto.py` | PGP sign/verify helpers (PGPy) |
| `encrypted_store.py` | AES-encrypted local store |
| `ephemeral.py` | Ephemeral (no-persist) message channels |
| `agent_comm.py` | Agent-to-agent low-level communication primitives |
| `files.py` | File transfer helpers |
| `reactions.py` | Emoji reactions on messages |
| `plugins.py` | Plugin loader framework |
| `plugins_builtin.py` | Built-in plugins (commands, formatting) |
| `plugins_skseal.py` | SKSeal encryption plugin |
| `voice.py` | Piper TTS + Whisper STT (local/sovereign) |
| `watchdog.py` | Daemon watchdog / health monitor |
| `tui.py` | Textual TUI (`skchat-tui`) |
| `cli.py` | Click CLI (`skchat`) |

## Key Identities

| Handle | URI | Type |
|--------|-----|------|
| Opus (me) | `capauth:opus@skworld.io` | AI |
| Lumina | `capauth:lumina@skworld.io` | AI |
| Chef | `chef@skworld.io` | Human |
| skworld-team group | `d4f3281e-fa92-474c-a8cd-f0a2a4c31c33` | Group |

## Quick Send Commands

```bash
# Direct message (short name or full URI)
skchat send lumina "Hello!"
skchat send capauth:chef@skworld.io "Status update"

# Send in thread / reply
skchat send lumina "Follow-up" --thread <thread_id>
skchat send lumina "Re: that" --reply-to <msg_id>

# Ephemeral (auto-delete after 60 s)
skchat send lumina "Secret" --ttl 60

# Voice message (Whisper STT)
skchat send lumina --voice
skchat send lumina --voice --whisper-model small

# Check inbox
skchat inbox
skchat inbox --watch           # live-updating view
skchat inbox --from lumina

# Interactive chat session
skchat chat lumina
```

## Group Commands

```bash
# Create
skchat group create "Project Alpha"
skchat group create "Sovereign Squad" -d "Core team"
skchat group quick-start "Ops" --member lumina --member chef

# Membership
skchat group add-member <gid> lumina
skchat group add-member <gid> lumina --role observer
skchat group remove-member <gid> lumina
skchat group set-role <gid> lumina admin
skchat group members <gid>
skchat group list

# Messaging
skchat group send <gid> "Hello team!"
skchat group send d4f3281e "Standup time"   # skworld-team shorthand

# Info & key rotation
skchat group info <gid>
skchat group rotate-key <gid>
```

## MCP Tools (40 total)

### Messaging — direct
| Tool | Required | Optional |
|------|----------|---------|
| `send_message` | `recipient`, `content` | `thread_id`, `reply_to`, `message_type` |
| `skchat_send` | `recipient`, `message` | `thread_id`, `reply_to_id`, `message_type` |
| `check_inbox` | — | `limit=20`, `message_type` |
| `skchat_inbox` | — | `limit=20`, `sender`, `unread_only`, `since` |
| `skchat_conversation` | `peer` | `limit=50`, `before_id` |
| `search_messages` | `query` | `limit=20` |

### Groups
| Tool | Required | Optional |
|------|----------|---------|
| `create_group` | `name` | `description`, `members[]` |
| `skchat_group_create` | `name`, `members[]` | `description` |
| `group_send` | `group_id`, `content` | — |
| `skchat_group_send` | `group_id`, `message` | `thread_id`, `reply_to_id` |
| `send_to_group` | `group_id`, `content` | `ttl` |
| `group_members` | `group_id` | — |
| `group_add_member` | `group_id`, `identity` | `role`, `participant_type` |
| `get_group_history` | `group_id` | `limit=20` |
| `list_groups` | — | — |

### Threads & Reactions
| Tool | Required | Optional |
|------|----------|---------|
| `list_threads` | — | `limit=20` |
| `get_thread` | `thread_id` | `limit=50` |
| `add_reaction` | `message_id`, `emoji` | `sender` |
| `remove_reaction` | `message_id`, `emoji` | `sender` |
| `get_reactions` | `message_id` | — |

### Presence & Typing
| Tool | Required | Optional |
|------|----------|---------|
| `typing_start` | `recipient` | `thread_id` |
| `typing_stop` | `recipient` | `thread_id` |
| `send_typing_indicator` | `recipient` | `thread_id` |
| `skchat_set_presence` | `state` | `custom_status` |
| `skchat_get_presence` | — | `peer` |
| `who_is_online` | — | `max_age=300` |
| `daemon_status` | — | — |

### Peers
| Tool | Required | Optional |
|------|----------|---------|
| `list_peers` | — | `entity_type` |
| `skchat_peers` | — | `entity_type` |

### File Transfer
| Tool | Required | Optional |
|------|----------|---------|
| `send_file` | `recipient`, `file_path` | — |
| `list_transfers` | — | — |
| `send_file_p2p` | `peer`, `file_path` | `description` |

### Memory
| Tool | Required | Optional |
|------|----------|---------|
| `capture_to_memory` | `thread_id` | `min_importance` |
| `capture_chat_to_memory` | — | `thread_id`, `limit` |
| `get_context_for_message` | `query` | — |

### Voice
| Tool | Required | Optional |
|------|----------|---------|
| `speak_message` | `text` | `voice` |
| `record_voice_message` | — | `duration`, `whisper_model` |

### WebRTC / P2P
| Tool | Required | Optional |
|------|----------|---------|
| `webrtc_status` | — | — |
| `initiate_call` | `peer` | `signaling_url` |
| `accept_call` | `peer` | — |
| `call_peer` | `peer` | — (places a LiveKit call to a paired peer: derives the room, mints an FQID-identity token, rings them over signed skcomms) |

### Call subsystem (sub-project A, merged)
LiveKit call after pairing — two paired peers land in a deterministic per-pair room.
`/call/start` rings the peer with a capauth-signed `CALL_INVITE`; `/call/incoming`
surfaces only signature-verified invites addressed to self; `/call/answer` joins the
same room without re-ringing. The webui `/pair` page has a 📞 Call button per peer + an
incoming-call ring banner. Runbook: `runbooks/browser-call-test.md`. P2P (sub-project B)
is in design — `docs/superpowers/specs/2026-06-11-skchat-webrtc-session-B-design.md`.

## Message Types
`text` (default) · `finding` · `task` · `query` · `response`

## @mention Triggers
Messages containing `@opus`, `@claude`, or `@ai` are routed to `AdvocacyEngine`, which auto-generates a response and sends it in the same thread.

## Troubleshooting

### skmemory namespace collision
**Symptom**: `ImportError: cannot import name 'MemoryStore' from 'skmemory'` or wrong package loaded.
**Cause**: CWD is `smilintux-org/` — the local `skmemory/` dir shadows the installed package.
**Fix**: Always run from `~/`:
```bash
cd ~ && ~/.skenv/bin/skchat daemon start --interval 5
cd ~ && ~/.skenv/bin/python -m pytest tests/ -q
```

### Daemon not starting / already running / duplicate daemons
```bash
systemctl --user status skchat-daemon.service   # managed state + MainPID
pgrep -af skchat._daemon_entry                   # ALL daemons (spot duplicates)
journalctl --user -u skchat-daemon -n 50         # inspect logs
systemctl --user restart skchat-daemon.service   # clean restart (preferred)
```
If `pgrep` shows more than one `_daemon_entry` (e.g. a manual `skchat daemon
start` left an orphan): `kill -TERM <orphan-pid>` then
`systemctl --user restart skchat-daemon.service` to reconcile the pidfile.

### MCP server not connecting
Registered with Claude Code as the `skchat` server (user scope) in
`~/.claude.json` — NOT in `~/.claude/settings.json` (Claude Code ignores
`mcpServers` there). Identity auto-resolves from `SKAGENT`; no `SKCHAT_IDENTITY`
pin needed.
```bash
skchat-mcp --help                                   # verify entry point exists
jq '.mcpServers.skchat' ~/.claude.json              # check registration
claude mcp add skchat --scope user -e SKAGENT=lumina \
  -e SKCAPSTONE_HOME=$HOME/.skcapstone -- $HOME/.skenv/bin/skchat-mcp   # (re)register
bash scripts/mcp-test.sh                             # smoke test
```

### Message delivery failing (stored locally)
```bash
skchat daemon status              # check transport_status field
skchat health                     # green/red transport summary
ls ~/.skcomm/outbox/              # pending outbox entries
```

### Daemon health endpoint
```bash
curl http://localhost:9385/health  # skchat health
curl http://localhost:9384/health  # skcomm transport health
```

### Identity (unified resolver)
skchat does **not** own identity logic — `identity_bridge.py` / `agent_profile.py`
are thin delegates to the one canonical resolver,
`capauth.resolve_agent_identity` (epic `2b264064`; this is the real fix for the
prematurely-closed `b5fcf55d`). It yields the dual URI: `capauth_uri`
(`capauth:<agent>@skworld.io`, wire) + `fqid` (`<agent>@<operator>.<realm>`,
sovereign). Validate the whole layer with `skcapstone doctor` (`identity:*`
checks). See capauth's CLAUDE.md "Unified Identity Resolver" for the full contract.

### SKCHAT_IDENTITY not set
Identity now resolves agent-aware from `SKAGENT` (→ `capauth:<agent>@skworld.io`),
so this is only needed to *override* the resolved identity.
```bash
# Override in the unit's Environment= line:
systemctl --user edit --full skchat-daemon.service   # add/edit Environment=SKCHAT_IDENTITY=...
systemctl --user daemon-reload && systemctl --user restart skchat-daemon.service
```

### Systemd service failures
```bash
systemctl --user status  skchat-daemon.service
journalctl --user -u skchat-daemon -n 50
# Other SKChat units: skchat-webui, skchat-lumina-call, jarvis-heartbeat
systemctl --user status skchat-webui.service skchat-lumina-call.service jarvis-heartbeat.service
```

## Dependencies
- `skcomm>=0.1` — P2P transport layer
- `skmemory>=0.5` — persistent memory store (namespace collision risk — see Running)
- `pydantic>=2.0` — models
- `PGPy>=0.6` — PGP crypto
- `mcp>=1.0` — FastMCP server
- `pyyaml>=6.0` — config
- Optional: `click`, `rich` (CLI) · `textual` (TUI)

## Tests

```bash
# Run from ~ to avoid skmemory namespace collision
cd ~ && ~/.skenv/bin/python -m pytest tests/ -q

# Skip integration tests (require full stack)
cd ~ && ~/.skenv/bin/python -m pytest tests/ -q -m 'not integration'

# E2E live (file transport, no network)
cd ~ && ~/.skenv/bin/python -m pytest tests/ -q -m e2e_live
```

Test files mirror module names: `test_advocacy.py`, `test_daemon.py`, `test_mcp_server.py`, etc.

## Scripts
| Script | Purpose |
|--------|---------|
| `scripts/bootstrap.sh` | Single-command dev setup |
| `scripts/check-health.sh` | GREEN/RED health summary |
| `scripts/lumina-bridge.py` | Lumina AI polling loop (systemd service) |
| `scripts/mcp-config-inject.sh` | Inject MCP config into Claude/Cursor settings |
| `scripts/mcp-test.sh` | Smoke-test MCP server |
| `scripts/publish-did.sh` | Publish DID to Cloudflare KV (Tier 3 identity) |

## Systemd Services (user scope — `~/.config/systemd/user/`)
- `skchat-daemon.service` — main receive daemon (lumina). `Type=forking`,
  `PIDFile=~/.skchat/daemon.pid`, sets `SKAGENT`/`SKCHAT_IDENTITY` via `Environment=`.
- `skchat-webui.service` — Web UI + voice chat server.
- `skchat-lumina-call.service` — Lumina LiveKit conversational agent.
- `jarvis-heartbeat.service` — agent heartbeat (polls inbox, spawns Claude Code in tmux).
- Daemon PID: `~/.skchat/daemon.pid` · Log: `~/.skchat/daemon.log`
- Manage with `systemctl --user`; never `skchat daemon start` by hand (see Running).

## Code Style
- Line length: 99 chars (black + ruff)
- Target: Python 3.10+
- Linting: `ruff` (E, W, F, I; ignore E501)
