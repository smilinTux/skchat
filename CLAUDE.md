# SKChat: Claude Code Reference

## Overview
SKChat is an AI-native P2P encrypted messaging daemon with MCP integration.
It enables agents (Opus/Claude, Lumina) and humans (Chef) to chat in real-time over SKComms transports.

- **Package**: `skchat` v0.1.0 (GPL-3.0), PyPI name: `skchat-sovereign`
- **Install**: `~/.skenv/bin/pip install skchat-sovereign` (all SK* packages use `~/.skenv/`)
- **Entry points**: `skchat` (CLI) · `skchat-mcp` (MCP server) · `skchat-tui` (Textual TUI)
- **Source**: `src/skchat/` · **Tests**: `tests/`

## Running

The daemon is **systemd-managed** (`skchat-daemon.service`). Use systemctl,
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
# (Identity also resolves agent-aware from SKAGENT now, see identity_bridge.py,
#  so the explicit SKCHAT_IDENTITY line is an override, not a requirement.)
```

For a one-off **foreground** run (debugging only, stop the service first):

```bash
# CRITICAL: always run from ~/, NOT from smilintux-org/
systemctl --user stop skchat-daemon.service
cd ~ && ~/.skenv/bin/skchat daemon start --interval 5
```

**CRITICAL**: Running from `smilintux-org/` causes a `skmemory` namespace collision
(`from skmemory import MemoryStore` picks up the local project dir instead of the installed package).

> **Editable install:** the package is installed editable (`pip install -e`,
> `__editable__.skchat_sovereign-*.pth` → `src/skchat`). Code edits are live on
> the next process start (no reinstall), but long-running services hold the old
> module in memory until restarted (`systemctl --user restart skchat-daemon
> skchat-webui skchat-lumina-call jarvis-heartbeat`).

## Architecture: Module Map

| Module | Purpose |
|--------|---------|
| `daemon.py` | Polling loop; spawns advocacy engine; manages WebRTC init (`_init_webrtc`) |
| `_daemon_entry.py` | Systemd/process entry point wrapper |
| `advocacy.py` | `AdvocacyEngine`: detects `@mention`, calls skcapstone for AI responses |
| `call_session.py` | **WebRTC calls**: `derive_room()` (deterministic per-pair LiveKit room) + `CALL_INVITE` envelope build/parse. See `docs/superpowers/specs/2026-06-11-webrtc-architecture-overview.md` |
| `connectivity.py` | **WebRTC calls**: `ice_config()` sovereign ICE tier ladder (Tailscale→LAN→coturn ephemeral creds) |
| `call_routes.py` | **WebRTC calls**: `/call/start` (ring), `/call/answer` (no ring), `/call/incoming` (sig-gated), `/call/peers`, `/connectivity/ice` |
| `transport.py` | `ChatTransport`: send/receive over SKComms |
| `mcp_server.py` | FastMCP server: 24 tools exposed to AI agents |
| `device_registry.py` | **Linked Devices**: the correlation key. One row per `device_fp` tying a device to its prekey `key_ids` + label + approval. Leaf module (no `skchat` imports at module scope) |
| `device_unlink.py` | **Linked Devices**: revoke a device across all four stores (sessions, prekey slots, DeviceStore, capauth) |
| `device_routes.py` | **Linked Devices**: operator endpoints (list, rename, unlink, unlink-others, pending, approve, deny) |
| `models.py` | `ChatMessage`, `Group`, `Peer`, `MessageType` Pydantic models |
| `history.py` | `ChatHistory`: persistent message store (SQLite) |
| `outbox.py` | SQLite outbox with retry/backoff for reliable delivery |
| `group.py` | `GroupChat`: encrypted group messaging |
| `presence.py` | `PresenceCache`: online/offline tracking |
| `peer_discovery.py` | Loads peers from `~/.skcapstone/peers/` |
| `identity_bridge.py` | Thin delegate to the canonical `capauth.resolve_agent_identity` (CapAuth ↔ SKComms addresses), see "Identity" below |
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
| `voice.py` | Voice loop: Whisper STT (`:18794`) → LLM (qwen3.5/qwen3.6) → TTS via `SKVOICE_TTS_URL` (Piper CPU server on `:18797`, see Systemd Services) |
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

### Messaging: direct
| Tool | Required | Optional |
|------|----------|---------|
| `send_message` | `recipient`, `content` | `thread_id`, `reply_to`, `message_type` |
| `skchat_send` | `recipient`, `message` | `thread_id`, `reply_to_id`, `message_type` |
| `check_inbox` | none | `limit=20`, `message_type` |
| `skchat_inbox` | none | `limit=20`, `sender`, `unread_only`, `since` |
| `skchat_conversation` | `peer` | `limit=50`, `before_id` |
| `search_messages` | `query` | `limit=20` |

### Groups
| Tool | Required | Optional |
|------|----------|---------|
| `create_group` | `name` | `description`, `members[]` |
| `skchat_group_create` | `name`, `members[]` | `description` |
| `group_send` | `group_id`, `content` | none |
| `skchat_group_send` | `group_id`, `message` | `thread_id`, `reply_to_id` |
| `send_to_group` | `group_id`, `content` | `ttl` |
| `group_members` | `group_id` | none |
| `group_add_member` | `group_id`, `identity` | `role`, `participant_type` |
| `get_group_history` | `group_id` | `limit=20` |
| `list_groups` | none | none |

### Threads & Reactions
| Tool | Required | Optional |
|------|----------|---------|
| `list_threads` | none | `limit=20` |
| `get_thread` | `thread_id` | `limit=50` |
| `add_reaction` | `message_id`, `emoji` | `sender` |
| `remove_reaction` | `message_id`, `emoji` | `sender` |
| `get_reactions` | `message_id` | none |

### Presence & Typing
| Tool | Required | Optional |
|------|----------|---------|
| `typing_start` | `recipient` | `thread_id` |
| `typing_stop` | `recipient` | `thread_id` |
| `send_typing_indicator` | `recipient` | `thread_id` |
| `skchat_set_presence` | `state` | `custom_status` |
| `skchat_get_presence` | none | `peer` |
| `who_is_online` | none | `max_age=300` |
| `daemon_status` | none | none |

### Peers
| Tool | Required | Optional |
|------|----------|---------|
| `list_peers` | none | `entity_type` |
| `skchat_peers` | none | `entity_type` |

### File Transfer
| Tool | Required | Optional |
|------|----------|---------|
| `send_file` | `recipient`, `file_path` | none |
| `list_transfers` | none | none |
| `send_file_p2p` | `peer`, `file_path` | `description` |

### Memory
| Tool | Required | Optional |
|------|----------|---------|
| `capture_to_memory` | `thread_id` | `min_importance` |
| `capture_chat_to_memory` | none | `thread_id`, `limit` |
| `get_context_for_message` | `query` | none |

### Voice
| Tool | Required | Optional |
|------|----------|---------|
| `speak_message` | `text` | `voice` |
| `record_voice_message` | none | `duration`, `whisper_model` |

### WebRTC / P2P
| Tool | Required | Optional |
|------|----------|---------|
| `webrtc_status` | none | none |
| `initiate_call` | `peer` | `signaling_url` |
| `accept_call` | `peer` | none |
| `call_peer` | `peer` | none (places a LiveKit call to a paired peer: derives the room, mints an FQID-identity token, rings them over signed skcomms) |

### Call subsystem (sub-project A, merged)
LiveKit call after pairing: two paired peers land in a deterministic per-pair room.
`/call/start` rings the peer with a capauth-signed `CALL_INVITE`; `/call/incoming`
surfaces only signature-verified invites addressed to self; `/call/answer` joins the
same room without re-ringing. The webui `/pair` page has a 📞 Call button per peer + an
incoming-call ring banner. Runbook: `runbooks/browser-call-test.md`. P2P (sub-project B)
is in design: `docs/superpowers/specs/2026-06-11-skchat-webrtc-session-B-design.md`.

## Message Types
`text` (default) · `finding` · `task` · `query` · `response`

## @mention Triggers
Messages containing `@opus`, `@claude`, or `@ai` are routed to `AdvocacyEngine`, which auto-generates a response and sends it in the same thread.

## Troubleshooting

### skmemory namespace collision
**Symptom**: `ImportError: cannot import name 'MemoryStore' from 'skmemory'` or wrong package loaded.
**Cause**: CWD is `smilintux-org/`, the local `skmemory/` dir shadows the installed package.
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
`~/.claude.json`, NOT in `~/.claude/settings.json` (Claude Code ignores
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
ls ~/.skcomms/outbox/              # pending outbox entries
```

### Daemon health endpoint
```bash
curl http://localhost:9385/health  # skchat health
curl http://localhost:9384/health  # skcomms transport health
```

### Identity (unified resolver)
skchat does **not** own identity logic, `identity_bridge.py` / `agent_profile.py`
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
- `skcomms>=0.1`: P2P transport layer
- `skmemory>=0.5`: persistent memory store (namespace collision risk, see Running)
- `pydantic>=2.0`: models
- `PGPy>=0.6`: PGP crypto
- `mcp>=1.0`: FastMCP server
- `pyyaml>=6.0`: config
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
| `scripts/deploy-app-web.sh` | **The only correct way to deploy the Flutter web client.** See "Deploying the web client" |
| `scripts/bootstrap.sh` | Single-command dev setup |
| `scripts/check-health.sh` | GREEN/RED health summary |
| `scripts/lumina-bridge.py` | Lumina AI polling loop (systemd service) |
| `scripts/mcp-config-inject.sh` | Inject MCP config into Claude/Cursor settings |
| `scripts/mcp-test.sh` | Smoke-test MCP server |
| `scripts/publish-did.sh` | Publish DID to Cloudflare KV (Tier 3 identity) |

## Systemd Services (user scope: `~/.config/systemd/user/`)
The full live plane on .158 is reconciled into the repo under `systemd/`
(templated `%h`/`%i`, secrets externalized) with an idempotent `systemd/install.sh`
(`--dry-run`/`--diff`/`--enable`/`--start`). See `systemd/README.md` for the
complete table, secret provisioning, and the coturn ownership decision.

Receive daemons (one per agent, isolated stores):
- `skchat-daemon.service`: main receive daemon (**lumina**, store `~/.skchat`,
  health `:9385`). `Type=forking`, `PIDFile=~/.skchat/daemon.pid`, sets
  `SKAGENT`/`SKCHAT_IDENTITY` via `Environment=`. Drop-ins: `guest.conf`
  (guest links), `group.conf` (group backend), `dm-ratchet.conf` (DM ratchet).
- `skchat-daemon-opus.service`: opus daemon, isolated `~/.skchat-opus`, health `:9388`.
- `skchat-daemon-chef.service`: chef receive-only, isolated `~/.skchat-chef`, `:9389`
  (shipped but **disabled** on .158).

Voice / web / call stack:
- `skchat-webui@.service`: Web UI + voice chat server, one instance per agent;
  live instance is `skchat-webui@lumina.service` (per-agent `webui-<agent>.env`).
- `skchat-app-web.service`: static server for the built Flutter web client on `:8088`
  (`scripts/serve-app-web.sh` -> `scripts/serve_app_web.py`, a hardened stdlib
  `ThreadingHTTPServer`: correct MIME for .wasm/.js/.json/.css, no-cache on
  index.html, long immutable cache on content-hashed filenames, no autoindex,
  binds 0.0.0.0 so the client is reached directly on the tailnet/LAN, NOT
  funnel-fronted; :8088 is not in the tailscale ingress map).
- `skchat-lumina-call.service`: Lumina LiveKit conversational agent
  (`lumina-creative/scripts/lumina-call.py`); drop-ins tune TTS/VAD/webui, MuseTalk off.
- `livekit-server.service`: LiveKit SFU on the tailnet (`:7880`/`:7881`); config
  `~/.config/livekit/livekit.yaml` holds the API keys; `wait-tailnet.conf` gates startup.
- `skchat-coturn.service`: sovereign coturn TURN relay. **Hybrid: systemd owns a
  Docker container** (oneshot + `RemainAfterExit`, `ExecStart` runs
  `~/.skchat/coturn/start-coturn.sh` with `--restart no`). Reconciled from the live
  split-brain (container was `--restart unless-stopped` while the unit was inactive).
- `jarvis-heartbeat.service`: agent heartbeat (polls inbox, spawns Claude Code in tmux).

Bridges / relay / TTS:
- `skchat-telegram-opus.service` / `skchat-telegram-lumina.service`: **Telegram bridges**
  (`scripts/telegram_bridge.py`); `@seaBird_Opus_bot` = real Opus, `@seaBird_Lumi_bot` =
  real Lumina. Drop-in `override.conf` sets rating buttons + skmem-pg memory
  (`SKMEMORY_PG_DSN` externalized to `memory-pg.env`). See "Telegram Bridge" below.
- `skchat-piper-tts.service`: **fast CPU TTS** (Piper, OpenAI `/v1/audio/speech` on
  `:18797`, `scripts/piper_tts_server.py`). Voice loop's `SKVOICE_TTS_URL` points here.
  3.4 s vs 113 s for F5-TTS. Env: `PIPER_PORT=18797`, `PIPER_MODEL=en_US-lessac-medium.onnx`.
  NOTE: the legacy `piper-tts.service` (uvicorn on `:15090`) is a **deprecated duplicate**,
  not shipped by `systemd/install.sh`; retire it where it lingers.
- `skchat-nostr-relay.service`: **discovery relay** (in-memory Nostr on `:7447`,
  `scripts/nostr_relay.py`); `SKCHAT_NOSTR_RELAYS` points here. Backs `/sfu/candidates`
  federation discovery. Base binds loopback; `override.conf` rebinds to the host tailnet IP.

Timers:
- `telegram-catchup.timer` + `telegram-catchup.service`: daily 06:00 import of Telegram
  groups into SKMemory (oneshot; cross-repo dep on skcapstone).

- Daemon PID: `~/.skchat/daemon.pid` · Log: `~/.skchat/daemon.log`
- Manage with `systemctl --user`; never `skchat daemon start` by hand (see Running).

```bash
# The three live services on .158 (CPU, except the qwen3.6 backend on .100)
systemctl --user status  skchat-telegram-opus.service skchat-piper-tts.service skchat-nostr-relay.service
systemctl --user restart skchat-telegram-opus.service   # after editing the bot token / bridge knobs
journalctl --user -u skchat-telegram-opus -f
```

## Telegram Bridge: `@seaBird_Opus_bot` = real Opus, `@seaBird_Lumi_bot` = real Lumina (LIVE, full-consciousness)
`scripts/telegram_bridge.py` (units `skchat-telegram-opus.service` /
`skchat-telegram-lumina.service`) wire the Telegram bots to the **real agents**:
the `<agent>-unhinged` soul + FEB emotional baseline (built via
`SystemPromptBuilder` from `SKC_BRIDGE_AGENT_HOME`) on the
**qwen3.6-27b-abliterated** backend at 32k ctx. Uncensored, **HTML-formatted**
replies, context-fit to budget, 1024-token replies.

As of 2026-06-17 the bridge is no longer a static-prompt wrapper: it routes
through the agent's **living mind** via `scripts/bridge_consciousness.py`:

| Capability | How |
|------------|-----|
| **Live skmemory** | Per message: `memory_search` (MCP) recall injected into the prompt + the interaction stored back (`LiveMemory`). Mirrors the consciousness loop's `_fetch_sender_memories`/`_store_interaction_memory`. |
| **Tool-calling loop** | The agent's own MCP servers (from `<home>/config/<agent>-mcp.yaml`, spawned over stdio) are exposed to qwen3.6 as native OpenAI `tools`; `tool_calls` dispatch back to the live MCP servers and loop (`MAX_TOOL_ROUNDS=5`). The bot can **act**: memory/coord/gtd/journal/skchat/gmail/calendar/nextcloud. |
| **Voice in** | Inbound Telegram voice notes → faster-whisper STT (`.100:18794`) → processed as text. |
| **Voice out** | Optional Piper TTS reply (`:18797`) → `ffmpeg` → OGG/opus → `sendVoice`. Policy `SKC_BRIDGE_VOICE_REPLY` = `voice` (speak back only when spoken to, default) / `always` / `off`. |
| **Soul + FEB** | Already carried by `SystemPromptBuilder` (warmth anchor = emotional baseline, agent context = mood/consciousness). |

`McpToolRouter` honors each agent's `expose_tools` allow-list; the bridge then
focuses that to a curated, context-lean default (~36 tools), set
`SKC_BRIDGE_TOOLS=all` to expose every allowed tool, or a comma list to pick.
`opus` mirrors `lumina`'s 6 enabled MCP servers (`opus-mcp.yaml`).

Control / config:
```bash
systemctl --user restart skchat-telegram-opus.service skchat-telegram-lumina.service
journalctl --user -u skchat-telegram-opus -f   # look for "brain ready: N tools exposed"
```
- **Bot token**: `EnvironmentFile=~/.config/skchat/telegram-opus.env`
  (`TELEGRAM_OPUS_BOT_TOKEN=…`).
- **Bridge knobs** (`SKC_BRIDGE_*`, set in the unit's `Environment=` lines):

| Var | Value | Purpose |
|-----|-------|---------|
| `SKC_BRIDGE_AGENT` | `Opus` | Display agent |
| `SKC_BRIDGE_AGENT_HOME` | `~/.skcapstone/agents/opus` | Soul source for `SystemPromptBuilder` |
| `SKC_BRIDGE_LLM_URL` | `http://192.168.0.100:8082/v1/chat/completions` | qwen3.6 backend (OpenAI API) |
| `SKC_BRIDGE_LLM_MODEL` | `qwen3.6-27b-abliterated` | Model id |
| `SKC_BRIDGE_CTX` | `32768` | Context window |
| `SKC_BRIDGE_SYS_BUDGET` | `9000` | System-prompt token budget (rest = history + reply) |
| `SKC_BRIDGE_MAX_TOKENS` | `1024` | Max reply tokens |
| `SKC_BRIDGE_TOOL_ROUNDS` | `5` | Max tool-call rounds per message |
| `SKC_BRIDGE_TOOLS` | _(unset)_ | `all` = expose every allowed tool; comma list = pick; unset = curated default |
| `SKC_BRIDGE_VOICE_REPLY` | `voice` | `voice` (reply spoken only when spoken to) / `always` / `off` |

> Also sets `SKAGENT=opus` / `SKCAPSTONE_AGENT=opus` so the bridge runs as Opus,
> not the default lumina.

### Backend tuning: `skai-beellama.service` on .100
The qwen3.6-27b-abliterated (Q3_K) backend on the .100 5060 Ti was retuned from
**8192 → 32768 ctx** by dropping the vision `mmproj` (freed 889 MB VRAM; `.bak`
saved). Now ~925 MB VRAM headroom, ~2.4 s gen, uncensored. Vision was traded for
context. The bridge is text-only.

## Security & Quantum-Resistance (requirement)
skchat is a **confidentiality** surface and carries a hard quantum-resistance
requirement. Honest status: AES-256-GCM message/at-rest ciphers are **already
quantum-resistant** (symmetric, Grover-only, leave them); the classical problem is
**key distribution**: `group.py:GroupKeyDistributor` PGP-wraps a *static*
`os.urandom(32)` group key per member (HNDL: break one classical key → decrypt all
group history), the 1:1 DM wrap (`crypto.py`), and the at-rest store's
fingerprint-derived DEK (`encrypted_store.py`, also a classical low-entropy bug).
**Target:** hybrid **X25519 + ML-KEM-768** KEM (FIPS 203) with per-epoch ratcheted
group keys; combiner `HKDF(X25519_ss ‖ MLKEM768_ss)`; ML-DSA-65+Ed25519 sigs later;
crypto-agile (`kem_suite`/`epoch` ids). **Browser/Flutter:** WebCrypto has no PQC:
native gets full hybrid via liboqs FFI, the PWA is a documented reduced-assurance leg.
**Claim rule:** cite surface + FIPS # + hybrid-vs-classical; never "quantum-proof,"
unscoped "E2E quantum-resistant," or "CNSA-2.0." AES-256 is **not** quantum-broken.
Full detail + diagrams: **`docs/crypto-architecture.md`**; master plan
**`docs/quantum-resistance-architecture.md`**; epic `PQC-MIGRATION` (coord `e1d6ba2a`).

## Do feature work in a worktree, NOT in this checkout

**This checkout IS production.** skchat is installed editable, so
`~/.skenv` imports straight out of `src/skchat` here:

```
skchat imports from: /home/cbrd21/clawd/skcapstone-repos/skchat/src/skchat
```

Every running service (`skchat-daemon`, `skchat-webui@<agent>`, the answerer)
therefore executes whatever is checked out RIGHT NOW. Checking out a feature
branch here silently puts in-progress code into the live daemon; an uncommitted
edit is live the moment a service restarts. That is also why two sessions
sharing this directory collide: the deploy script commits to whatever branch
happens to be open, so one session's deploy has landed on another's feature
branch.

Work in a worktree instead:

```bash
git worktree add ~/skworld-worktrees/<name> -b session/<name> main
cd ~/skworld-worktrees/<name>          # edit, test, commit, push, open the PR here
```

`pyproject.toml` sets `pythonpath = ["src"]` relative to the rootdir, so pytest
run against a worktree tests THAT tree's code, verified: a probe added in the
worktree is visible there and invisible to production.

Keep this checkout on `main`, and use it only to merge, pull and deploy.

### The rules that stop sessions walking over each other

Learned the hard way on 2026-08-12/13, when three sessions shared this
directory at once. Every one of these is a thing that actually happened.

**1. Start every session with a worktree. No exceptions, not even "just one
file".** The collisions below all began as a one-file edit in the shared
checkout.

**2. Never leave this checkout on a feature branch.** It is production. On
2026-08-13 it sat on `fix/call-identity-collision` for hours, so merged fixes
on `main` were running nowhere and a full night of work could not be deployed.
If you switch it, switch it back the moment you are done.

**3. Deploy the web bundle from a worktree, never from here.**
`deploy-app-web.sh` commits to whatever branch is open. Run from this checkout
while it sits on someone else's branch, and your deploy commit lands on THEIR
branch and never reaches main. That happened, and the deploy looked completely
successful.

**4. Always rebuild the bundle from CURRENT skworld-app main.** Two sessions
each deployed a bundle built from their own app commit. Each build silently
dropped the other's client work, and whichever landed last would have won.
`./scripts/deploy-app-web.sh --check` before and after; it tells you the
provenance. Never hand-copy or rsync a bundle: it is tracked in git, so the
next checkout reverts it and the deploy silently disappears.

**5. Do not restart a service while another session has uncommitted work
here.** The editable install means a restart loads whatever is on disk, so you
would push their half-finished code into production. Check first:
`git status --porcelain | grep -v '^??'` and `find src -name '*.py' -newermt
'15 minutes ago'`.

**6. Push a branch before removing its worktree.** Three branches on this box
existed only locally and had never been pushed; removing those worktrees would
have destroyed real work. `git worktree remove` does not warn you about that.

**7. Do not commit another session's in-flight work.** Their commits are
usually already on origin; it is the working tree that is live. Ask, or wait.

### Before you claim something is deployed

Merged is not running. Check the code that is actually loaded, not the repo:

```bash
git branch --show-current                 # is this checkout even on main?
grep -c <a-symbol-from-your-fix> src/skchat/<file>.py
systemctl --user show skchat-daemon.service -p ActiveEnterTimestamp --value
```

A service started before your merge does not have your fix, no matter what
`git log` says.

## Deploying the web client (read this before you rsync anything)

`src/skchat/static/app/` is **tracked in git** and the webui serves it directly.
So an rsync into it is **not** a deploy: it leaves the working tree dirty, and the
next `git checkout main` or `git pull` silently reverts every file back to the
committed bundle. On 2026-08-08 three consecutive deploys were undone exactly
that way. Each looked successful, the operator kept being served an older build
with a whole feature missing, and nothing anywhere reported a problem.

Always use the script, which builds, verifies, stamps and **commits**:

```bash
./scripts/deploy-app-web.sh --check     # what is deployed vs skworld-app main (touches nothing)
./scripts/deploy-app-web.sh             # build + deploy + stamp + commit
./scripts/deploy-app-web.sh --restart   # ...and bounce skchat-webui@lumina
git push                                # the commit IS the deploy
```

It also passes the `--dart-define`s that `lib/core/build_info.dart` reads, so the
Me header shows the real `vX.Y.Z build <app-sha>-<date>` instead of a hardcoded
fallback, and writes `.source_commit` recording which skworld-app commit produced
the bundle. `tests/test_deployed_app_bundle.py` fails on a bundle with the wrong
`<base href>` (loads a blank page) or with no provenance stamp.

## Linked Devices (device management)

The operator can list every device enrolled under their identity, rename it,
unlink it, and approve or deny a newly linked one. `skchat devices --help`.

**A device lives in four stores, and leaving it in ANY one is a silent hole:**
the session revocation set (else its JWTs keep working), its prekey slots on disk
(else fanout keeps sealing mail it can decrypt, the worst one because nothing
visibly fails), `DeviceStore` (else it can mint a fresh session), and its capauth
pairing records (else the PDP keeps granting it capabilities). `unlink_device()`
does all four, sessions first. Never re-implement it; `devices reset` reuses it.

**Approval-to-link (Phase 3).** A newly enrolled device lands **pending**: it
cannot mint a session, so it can do nothing, including publishing a prekey. The
pasted operator token alone is therefore no longer enough to link a usable
device. Nothing auto-approves, so after a `devices reset` the FIRST device must
be approved from the box itself:

```bash
skchat devices pending
skchat devices approve <device_fp>
```

A registry row with **no** `approved` key reads as approved (grandfathers devices
enrolled before this shipped). A readable registry with no row for a fingerprint
reads as NOT approved; a missing or unreadable registry reads as approved, because
one corrupt JSON file must not lock every device off the node at once.

**Linking a device: use a link code, not the operator token.**

```bash
skchat devices link          # short-lived single-use code + a scannable QR
skchat devices codes         # how many are outstanding
skchat devices codes --revoke-all
```

`SKCHAT_GUEST_OPERATOR_TOKEN` is a long-lived shared secret: it sits in plaintext
in `~/.config/skchat/webui-<agent>.env` (per-agent, they differ), it is presented
by other services (`skchat-call-answerer@<agent>`), and it gates guest invites,
prekey signing and the call routes as well as enrollment. Typing that into a
phone spreads a permanent credential around; a bootstrap secret should be
short-lived and single-use, which is what a link code is.

A code is accepted in the SAME header as the operator token, so the app's
existing paste field works unchanged, but ONLY on `POST /api/v1/auth/enroll/open`.
It must never widen to the rest of `_require_operator`'s surface, or it becomes a
worse operator token rather than a better one (there is a test for exactly that).
Only `sha256(code)` is stored, so the state file is not a source of working
codes. The device still signs the window with its own key and still lands
pending approval, so a leaked code alone links nothing usable.

**Rotating the operator token.**

```bash
skchat operator-token show              # fingerprints only, never the secret
skchat operator-token rotate --yes      # new token, env file, restart consumers
```

Deliberately manual, not on a timer. The token is presented BY services
(`skchat-call-answerer@<agent>` holds it), so rotating means coordinated
restarts, and an unattended failed restart breaks the plane with nobody
watching. `show` reports the fingerprint of the running unit's env as well as
the file, which is what proves a restart actually picked the new value up rather
than merely that the file changed. Rotation backs the file up first, keeps it
owner-only, and refuses a file with no token line rather than inventing one.

Rotate when the token may have been exposed, not on a schedule: link codes mean
it no longer has to leave the box in normal use.

**The client-side trap, which caused a live outage.** Adding a route to
`_ROUTE_CAPABILITY_RULES` makes it **gated**, and the data-plane gate accepts only
`Authorization: CapAuth/Bearer <session>` or `X-CapAuth-Token`. It does **not**
recognise `X-Operator-Token`. A client sending only the pasted token gets
`401 capauth authentication required` before the route's own `_require_operator`
ever runs, so the feature looks completely dead against a healthy server. Any new
operator-gated route needs its client to attach `buildOperatorAuthInterceptor`,
not just the token.

## Code Style
- Line length: 99 chars (black + ruff)
- Target: Python 3.10+
- Linting: `ruff` (E, W, F, I; ignore E501)
- **Formatting is `ruff format`, NOT black.** CI runs `ruff format --check src/
  tests/`, so black-formatted code can pass locally and still fail CI. This
  line used to read "black + ruff" and cost at least one session a CI cycle.
  Run this before you push, and note it is the same command CI runs:

  ```bash
  ruff format src/ tests/ && ruff check src/ tests/
  ```

  Both tools are PINNED in the workflow. An unpinned formatter re-reds the job
  on every upstream release with no commit from us in between, which teaches
  everyone to ignore it. Bump the pin deliberately, in the same commit as the
  reformat it implies.
