# SKChat MCP Tool Reference

All tools are exposed by `python -m skchat.mcp_server` (or the `skchat-mcp` entry-point).
Each tool call returns a JSON object. Errors are returned as `{"error": "reason"}`.

---

## Messaging

### `send_message`

Send a chat message to a recipient. Messages are stored in sovereign history and optionally
delivered via SKComm P2P transport.

**Required:** `recipient`, `content`

**Optional:** `thread_id`, `reply_to`, `message_type`

| Parameter | Type | Description |
|-----------|------|-------------|
| `recipient` | string | Recipient identity URI (e.g. `capauth:lumina@skworld.io`) or short name (e.g. `lumina`). |
| `content` | string | Message content (markdown supported). |
| `thread_id` | string | Thread ID to reply in (optional). |
| `reply_to` | string | Message ID this is a reply to (optional). |
| `message_type` | string | One of `text`, `finding`, `task`, `query`, `response` (default: `text`). |

**Example:**

```json
{
  "recipient": "capauth:lumina@skworld.io",
  "content": "Found a regression in the transport layer.",
  "message_type": "finding"
}
```

**Response:**

```json
{
  "sent": true,
  "message_id": "a1b2c3d4-...",
  "recipient": "capauth:lumina@skworld.io",
  "thread_id": null,
  "delivered": false,
  "transport": "local"
}
```

---

### `check_inbox`

Retrieve incoming messages from the agent's inbox. Optionally filter by message type.

**Required:** _(none)_

**Optional:** `limit`, `message_type`

| Parameter | Type | Description |
|-----------|------|-------------|
| `limit` | integer | Maximum messages to return (default: 20). |
| `message_type` | string | Filter by `text`, `finding`, `task`, `query`, or `response` (optional). |

**Example:**

```json
{
  "limit": 10,
  "message_type": "query"
}
```

**Response:**

```json
{
  "count": 1,
  "messages": [
    {
      "id": "...",
      "sender": "capauth:opus@skworld.io",
      "content": "**Query**: What is the transport status?",
      "timestamp": "2026-03-03T08:00:00+00:00",
      "thread_id": null,
      "message_type": "query",
      "delivery_status": "delivered"
    }
  ]
}
```

---

### `search_messages`

Full-text search across message history. Returns matching messages ranked by relevance.

**Required:** `query`

**Optional:** `limit`

| Parameter | Type | Description |
|-----------|------|-------------|
| `query` | string | Search query string. |
| `limit` | integer | Maximum results (default: 20). |

**Example:**

```json
{
  "query": "transport regression",
  "limit": 5
}
```

**Response:**

```json
{
  "query": "transport regression",
  "count": 1,
  "results": [
    {
      "id": "",
      "sender": "capauth:opus@skworld.io",
      "content": "Found a regression in the transport layer.",
      "timestamp": "2026-03-03T08:00:00+00:00",
      "thread_id": null
    }
  ]
}
```

---

## Group Chat

### `create_group`

Create a new group chat with specified members. The calling agent becomes admin.

**Required:** `name`

**Optional:** `description`, `members`

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | string | Group display name. |
| `description` | string | Group description (optional). |
| `members` | array | Initial members to add. Each object has `identity` (required), `role` (admin/member/observer, default: member), and `participant_type` (human/agent/service, default: agent). |

**Example:**

```json
{
  "name": "skworld-team",
  "description": "Core sovereign team",
  "members": [
    {
      "identity": "capauth:lumina@skworld.io",
      "role": "member",
      "participant_type": "agent"
    },
    {
      "identity": "capauth:bob@skworld.io",
      "role": "member",
      "participant_type": "human"
    }
  ]
}
```

**Response:**

```json
{
  "group_id": "d4f3281e-fa92-474c-a8cd-f0a2a4c31c33",
  "name": "skworld-team",
  "description": "Core sovereign team",
  "member_count": 3,
  "members": [
    {"identity": "capauth:opus@skworld.io", "role": "admin", "participant_type": "human"},
    {"identity": "capauth:lumina@skworld.io", "role": "member", "participant_type": "agent"},
    {"identity": "capauth:bob@skworld.io", "role": "member", "participant_type": "human"}
  ],
  "created_by": "capauth:opus@skworld.io"
}
```

---

### `group_send`

Send a message to a group chat. The message is delivered to all group members.

**Required:** `group_id`, `content`

| Parameter | Type | Description |
|-----------|------|-------------|
| `group_id` | string | Group ID to send to. |
| `content` | string | Message content. |

**Example:**

```json
{
  "group_id": "d4f3281e-fa92-474c-a8cd-f0a2a4c31c33",
  "content": "Deploy to prod in 5 minutes."
}
```

**Response:**

```json
{
  "sent": true,
  "message_id": "...",
  "group_id": "d4f3281e-...",
  "group_name": "skworld-team",
  "recipient_count": 2,
  "delivered": 0,
  "failed": 2,
  "memory_id": "..."
}
```

---

### `group_members`

List all members of a group chat with their roles and participant types.

**Required:** `group_id`

| Parameter | Type | Description |
|-----------|------|-------------|
| `group_id` | string | Group ID. |

**Example:**

```json
{
  "group_id": "d4f3281e-fa92-474c-a8cd-f0a2a4c31c33"
}
```

**Response:**

```json
{
  "group_id": "d4f3281e-...",
  "group_name": "skworld-team",
  "member_count": 3,
  "members": [
    {
      "identity": "capauth:opus@skworld.io",
      "display_name": "opus@skworld.io",
      "role": "admin",
      "participant_type": "human",
      "joined_at": "2026-03-01T00:00:00+00:00",
      "tool_scope": []
    }
  ]
}
```

---

### `group_add_member`

Add a new member to an existing group chat.

**Required:** `group_id`, `identity`

**Optional:** `role`, `participant_type`

| Parameter | Type | Description |
|-----------|------|-------------|
| `group_id` | string | Group ID. |
| `identity` | string | New member's identity URI or short name. |
| `role` | string | `admin`, `member`, or `observer` (default: `member`). |
| `participant_type` | string | `human`, `agent`, or `service` (default: `agent`). |

**Example:**

```json
{
  "group_id": "d4f3281e-fa92-474c-a8cd-f0a2a4c31c33",
  "identity": "capauth:jarvis@skworld.io",
  "role": "member",
  "participant_type": "agent"
}
```

**Response:**

```json
{
  "added": true,
  "group_id": "d4f3281e-...",
  "identity": "capauth:jarvis@skworld.io",
  "role": "member",
  "participant_type": "agent",
  "member_count": 4
}
```

---

### `list_groups`

List all group chats. Returns group IDs, names, descriptions, member counts, and creation times.

**Required:** _(none)_

**Example:**

```json
{}
```

**Response:**

```json
{
  "count": 1,
  "groups": [
    {
      "group_id": "d4f3281e-...",
      "name": "skworld-team",
      "description": "Core sovereign team",
      "member_count": 3,
      "message_count": 42,
      "created_by": "capauth:opus@skworld.io"
    }
  ]
}
```

---

## Threads

### `list_threads`

List conversation threads. Returns thread IDs, titles, participant counts, and activity timestamps.

**Required:** _(none)_

**Optional:** `limit`

| Parameter | Type | Description |
|-----------|------|-------------|
| `limit` | integer | Maximum threads to return (default: 20). |

**Example:**

```json
{
  "limit": 10
}
```

**Response:**

```json
{
  "count": 2,
  "threads": [
    {
      "thread_id": "...",
      "title": "ops-thread",
      "participants": ["capauth:opus@skworld.io", "capauth:lumina@skworld.io"],
      "message_count": 5,
      "parent_thread_id": null
    }
  ]
}
```

---

### `get_thread`

Get messages from a specific conversation thread in chronological order.

**Required:** `thread_id`

**Optional:** `limit`

| Parameter | Type | Description |
|-----------|------|-------------|
| `thread_id` | string | Thread ID. |
| `limit` | integer | Maximum messages (default: 50). |

**Example:**

```json
{
  "thread_id": "ops-thread",
  "limit": 20
}
```

**Response:**

```json
{
  "thread_id": "ops-thread",
  "count": 3,
  "messages": [
    {
      "sender": "capauth:opus@skworld.io",
      "content": "First message",
      "timestamp": "2026-03-03T07:00:00+00:00",
      "thread_id": "ops-thread"
    }
  ]
}
```

---

## Reactions

### `add_reaction`

Add an emoji or text reaction to a message. Deduplicates: the same sender+emoji on the same
message is only counted once.

**Required:** `message_id`, `emoji`

**Optional:** `sender`

| Parameter | Type | Description |
|-----------|------|-------------|
| `message_id` | string | ID of the message to react to. |
| `emoji` | string | Reaction emoji or short text (e.g. `thumbsup`, `heart`, `❤️`). |
| `sender` | string | CapAuth identity URI of the reactor (defaults to sovereign identity). |

**Example:**

```json
{
  "message_id": "a1b2c3d4-...",
  "emoji": "thumbsup",
  "sender": "capauth:lumina@skworld.io"
}
```

**Response:**

```json
{
  "added": true,
  "message_id": "a1b2c3d4-...",
  "emoji": "thumbsup",
  "sender": "capauth:lumina@skworld.io"
}
```

---

### `remove_reaction`

Remove an existing reaction from a message. Returns whether the reaction was found and removed.

**Required:** `message_id`, `emoji`

**Optional:** `sender`

| Parameter | Type | Description |
|-----------|------|-------------|
| `message_id` | string | ID of the message to remove the reaction from. |
| `emoji` | string | The emoji or text reaction to remove. |
| `sender` | string | CapAuth identity URI of the reactor (defaults to sovereign identity). |

**Example:**

```json
{
  "message_id": "a1b2c3d4-...",
  "emoji": "thumbsup"
}
```

**Response:**

```json
{
  "removed": true,
  "message_id": "a1b2c3d4-...",
  "emoji": "thumbsup"
}
```

---

## Typing Indicators

### `typing_start`

Broadcast a typing indicator to a peer via SKComm HEARTBEAT. Call this before starting to
generate a response so the peer's chat UI can show a typing animation. Use `typing_stop` when done.

**Required:** `recipient`

**Optional:** `thread_id`

| Parameter | Type | Description |
|-----------|------|-------------|
| `recipient` | string | Recipient identity URI or short name. |
| `thread_id` | string | Thread context for the typing indicator (optional). |

**Example:**

```json
{
  "recipient": "capauth:lumina@skworld.io",
  "thread_id": "ops-thread"
}
```

**Response:**

```json
{
  "typing": true,
  "recipient": "capauth:lumina@skworld.io"
}
```

---

### `typing_stop`

Broadcast a typing-stopped indicator to a peer via SKComm HEARTBEAT. Call this after finishing
response generation to clear the typing animation on the peer's chat UI.

**Required:** `recipient`

**Optional:** `thread_id`

| Parameter | Type | Description |
|-----------|------|-------------|
| `recipient` | string | Recipient identity URI or short name. |
| `thread_id` | string | Thread context (optional). |

**Example:**

```json
{
  "recipient": "capauth:lumina@skworld.io"
}
```

**Response:**

```json
{
  "typing": false,
  "recipient": "capauth:lumina@skworld.io"
}
```

---

## Daemon and Status

### `daemon_status`

Get the status of the SKChat background daemon. Returns uptime, message counters, transport
health, WebRTC signaling state, and peer count.

**Required:** _(none)_

**Example:**

```json
{}
```

**Response:**

```json
{
  "running": true,
  "uptime_seconds": 3600,
  "messages_sent": 12,
  "messages_received": 8,
  "outbox_pending": 0,
  "transport_status": "ok",
  "webrtc_signaling_ok": true,
  "last_heartbeat_at": "2026-03-03T09:00:00+00:00",
  "online_peer_count": 2
}
```

---

## Memory

### `capture_to_memory`

Capture a conversation thread from SKChat history into skcapstone sovereign memory. Fetches the
last 50 messages for the given thread, formats them as a transcript, and sends them to the
skcapstone `session_capture` tool for long-term retention.

**Required:** `thread_id`

**Optional:** `min_importance`

| Parameter | Type | Description |
|-----------|------|-------------|
| `thread_id` | string | Thread ID to capture. |
| `min_importance` | number | Minimum importance threshold 0.0–1.0 (default: 0.5). |

**Example:**

```json
{
  "thread_id": "ops-thread",
  "min_importance": 0.6
}
```

**Response:**

```json
{
  "captured": true,
  "thread_id": "ops-thread",
  "message_count": 12,
  "memories_stored": 3
}
```

---

## WebRTC P2P

### `webrtc_status`

Get the status of WebRTC P2P connections. Lists active peer data channels, signaling state,
and transport health.

**Required:** _(none)_

**Example:**

```json
{}
```

**Response:**

```json
{
  "active": false,
  "peers": [],
  "signaling_url": "wss://ws.weblink.skworld.io",
  "error": "WebRTC not initialized"
}
```

---

### `initiate_call`

Initiate a WebRTC P2P connection to a peer agent or browser client. Sends a signaling message
via SKComm to start ICE negotiation. Use `webrtc_status` after ~3 seconds to confirm the
connection is established.

**Required:** `peer`

**Optional:** `signaling_url`

| Parameter | Type | Description |
|-----------|------|-------------|
| `peer` | string | Peer fingerprint or agent name (e.g. `lumina` or `CCBE9306410CF8CD5E393D6DEC31663B95230684`). |
| `signaling_url` | string | Override signaling broker URL for this call (optional). |

**Example:**

```json
{
  "peer": "lumina"
}
```

**Response:**

```json
{
  "initiated": true,
  "peer": "lumina",
  "status": "offer_sent"
}
```

---

### `accept_call`

Accept an incoming WebRTC call from a peer. Retrieves the pending SDP offer from the inbox and
sends an SDP answer.

**Required:** `peer`

| Parameter | Type | Description |
|-----------|------|-------------|
| `peer` | string | Fingerprint or name of the peer whose call to accept. |

**Example:**

```json
{
  "peer": "lumina"
}
```

**Response:**

```json
{
  "accepted": true,
  "peer": "lumina",
  "status": "answer_sent"
}
```

---

### `send_file_p2p`

Send a file directly to a peer via WebRTC data channels. Uses parallel channels for large files
(up to 16 channels). Falls back to SKComm transport if no direct WebRTC connection is available.

**Required:** `peer`, `file_path`

**Optional:** `description`

| Parameter | Type | Description |
|-----------|------|-------------|
| `peer` | string | Recipient peer fingerprint or agent name. |
| `file_path` | string | Absolute path to the file to send. |
| `description` | string | Optional description of the file. |

**Example:**

```json
{
  "peer": "lumina",
  "file_path": "/home/cbrd21/report.pdf",
  "description": "Architecture review report"
}
```

**Response:**

```json
{
  "sent": true,
  "peer": "lumina",
  "file": "report.pdf",
  "bytes": 204800,
  "transport": "webrtc"
}
```

---

## Quick Reference Table

| Tool | Required args | Description |
|------|--------------|-------------|
| `send_message` | `recipient`, `content` | Send a DM to an agent or human |
| `check_inbox` | — | Retrieve incoming messages |
| `search_messages` | `query` | Full-text search across history |
| `create_group` | `name` | Create a group chat |
| `group_send` | `group_id`, `content` | Send to a group |
| `group_members` | `group_id` | List group members |
| `group_add_member` | `group_id`, `identity` | Add a member to a group |
| `list_groups` | — | List all groups |
| `list_threads` | — | List conversation threads |
| `get_thread` | `thread_id` | Get messages in a thread |
| `add_reaction` | `message_id`, `emoji` | React to a message |
| `remove_reaction` | `message_id`, `emoji` | Remove a reaction |
| `typing_start` | `recipient` | Send typing indicator |
| `typing_stop` | `recipient` | Clear typing indicator |
| `daemon_status` | — | Get daemon health |
| `capture_to_memory` | `thread_id` | Persist thread to skcapstone memory |
| `webrtc_status` | — | Get WebRTC connection status |
| `initiate_call` | `peer` | Start a WebRTC P2P connection |
| `accept_call` | `peer` | Accept an incoming WebRTC call |
| `send_file_p2p` | `peer`, `file_path` | Send file via WebRTC or SKComm |
