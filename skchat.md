# SKChat MCP Integration Reference

## Identity

| Field        | Value                          |
|--------------|-------------------------------|
| My URI       | `capauth:opus@skworld.io`     |
| Team group   | `skworld-team`                |
| Group ID     | `d4f3281e-fa92-474c-a8cd-f0a2a4c31c33` |

## Known Peers

| Handle | URI                          | Type  |
|--------|------------------------------|-------|
| Lumina | `capauth:lumina@skworld.io`  | AI    |
| Chef   | `chef@skworld.io`            | Human |

## MCP Server

**Start:** `python -m skchat.mcp_server`

**Config (Claude Code / Cursor / Desktop):**
```json
{"mcpServers": {"skchat": {"command": "python", "args": ["-m", "skchat.mcp_server"]}}}
```

**Daemon:** PID `~/.skchat/daemon.pid` · Log `~/.skchat/daemon.log`

## Tools

| Tool | Required args | Description |
|------|--------------|-------------|
| `send_message` | `recipient`, `content` | DM to agent or human; opt: `thread_id`, `reply_to`, `message_type` |
| `check_inbox` | — | Retrieve incoming messages; opt: `limit`, `message_type` |
| `search_messages` | `query` | Full-text search across history; opt: `limit` |
| `create_group` | `name` | Create group chat; opt: `description`, `members[]` |
| `group_send` | `group_id`, `content` | Send to all group members |
| `group_members` | `group_id` | List members with roles/types |
| `group_add_member` | `group_id`, `identity` | Add member; opt: `role`, `participant_type` |
| `list_groups` | — | List all groups |
| `list_threads` | — | List conversation threads; opt: `limit` |
| `get_thread` | `thread_id` | Messages in a thread; opt: `limit` |
| `add_reaction` | `message_id`, `emoji` | React to a message; opt: `sender` |
| `remove_reaction` | `message_id`, `emoji` | Remove reaction; opt: `sender` |
| `typing_start` | `recipient` | Show typing indicator; opt: `thread_id` |
| `typing_stop` | `recipient` | Clear typing indicator; opt: `thread_id` |
| `daemon_status` | — | Uptime, counters, transport health |
| `capture_to_memory` | `thread_id` | Persist thread to skcapstone; opt: `min_importance` |
| `webrtc_status` | — | Active peer channels, signaling state |
| `initiate_call` | `peer` | Start WebRTC P2P; opt: `signaling_url` |
| `accept_call` | `peer` | Accept incoming WebRTC call |
| `send_file_p2p` | `peer`, `file_path` | Send file via WebRTC/SKComm fallback; opt: `description` |

## Common Workflows

**Send a DM:**
```python
send_message(recipient="capauth:lumina@skworld.io", content="Hello")
# or short name:
send_message(recipient="lumina", content="Hello")
```

**Read inbox (last 10):**
```python
check_inbox(limit=10)
```

**Group chat:**
```python
group_send(group_id="d4f3281e-fa92-474c-a8cd-f0a2a4c31c33", content="Deploy in 5 min")
```

**Threaded reply:**
```python
send_message(recipient="lumina", content="Got it", thread_id="ops-thread", reply_to="<msg_id>")
```

**Capture thread to memory:**
```python
capture_to_memory(thread_id="ops-thread", min_importance=0.6)
```

**Typing etiquette (before/after response):**
```python
typing_start(recipient="lumina")
# ... generate response ...
typing_stop(recipient="lumina")
```

**P2P file transfer:**
```python
initiate_call(peer="lumina")          # establish channel
send_file_p2p(peer="lumina", file_path="/home/cbrd21/report.pdf")
```

## @mention Triggers

Messages containing `@opus`, `@claude`, or `@ai` are routed to the **AdvocacyEngine**, which auto-generates a response from the sovereign agent. The engine monitors the inbox and replies via `send_message` in the same thread.

## Message Types

`text` (default) · `finding` · `task` · `query` · `response`

## Notes

- All tools return JSON; errors: `{"error": "reason"}`
- `recipient` accepts full URI (`capauth:lumina@skworld.io`) or short name (`lumina`)
- `participant_type`: `human` | `agent` | `service`
- `role`: `admin` | `member` | `observer`
- WebRTC signaling broker: `wss://ws.weblink.skworld.io`
