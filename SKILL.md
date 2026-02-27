# SKChat

AI-native encrypted P2P chat for humans and AI agents — sovereign communication over SKComm transports.

---

## Install

**Minimal (library only):**

```bash
pip install skchat
```

**With CLI (recommended):**

```bash
pip install "skchat[all]"
```

This installs the optional `click` and `rich` dependencies required for the `skchat` terminal command.

**From source:**

```bash
git clone https://github.com/smilinTux/skchat
cd skchat
pip install -e ".[all]"
```

**Requirements:** Python >= 3.11, PGPy, pydantic, pyyaml

---

## Quick Start

**Send a message:**

```bash
skchat send lumina "Hey, check the deploy status"
skchat send capauth:bob@skworld.io "Hello Bob" --thread my-thread
skchat send jarvis "Self-destruct in 60s" --ttl 60
```

**Read your inbox (locally stored messages):**

```bash
skchat inbox
skchat inbox --limit 5
skchat inbox --thread my-thread
```

**Poll transports for new messages (fetch + store):**

```bash
skchat receive
```

**Live-watch incoming messages:**

```bash
skchat watch
skchat watch --interval 2
```

---

## CLI Commands

### send

Send a message to a recipient. Composes a `ChatMessage`, stores it in local history, and (when a transport is configured) delivers it via SKComm.

The recipient can be a friendly peer name (`lumina`, `jarvis`) or a full CapAuth URI (`capauth:bob@skworld.io`). Peer names are resolved from the skcapstone peer registry.

```
skchat send RECIPIENT MESSAGE [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `--thread`, `-t` | None | Thread ID for conversation grouping |
| `--reply-to`, `-r` | None | Message ID this replies to |
| `--ttl` | None | Seconds until auto-delete (ephemeral message) |
| `--content-type` | `markdown` | Content type: `plain` or `markdown` |

**Examples:**

```bash
skchat send lumina "Deploy complete"
skchat send capauth:bob@skworld.io "Ready?" --thread standup
skchat send jarvis "Disappears in 30s" --ttl 30
skchat send ava "## Report\nAll systems nominal" --content-type markdown
```

---

### inbox

Show locally-stored messages. This command reads from the local SKMemory-backed history — it does NOT poll any transport. Run `skchat receive` first to fetch messages from transports.

```
skchat inbox [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `--limit`, `-n` | 20 | Max messages to show |
| `--thread`, `-t` | None | Filter by thread ID |

**Examples:**

```bash
skchat inbox
skchat inbox --limit 5
skchat inbox --thread standup
```

---

### receive

Poll all configured SKComm transports for incoming messages, then store them in local history. This is the command to run to actually fetch new messages from peers.

```
skchat receive
```

No flags. Returns immediately after polling.

**Examples:**

```bash
skchat receive
```

---

### watch

Live-watch incoming messages. Continuously polls SKComm at a configurable interval and displays messages as they arrive. Uses `rich.live` for an updating terminal display. Press Ctrl+C to stop.

```
skchat watch [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `--interval`, `-i` | 5.0 | Poll interval in seconds |
| `--limit`, `-n` | 20 | Max messages to show per poll |

**Examples:**

```bash
skchat watch
skchat watch --interval 2
skchat watch -i 10 -n 50
```

---

### history

Show full conversation history with a specific participant. Displays messages exchanged between you and the given peer, sorted newest first. Accepts friendly peer names or full CapAuth URIs.

```
skchat history PARTICIPANT [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `--limit`, `-n` | 30 | Max messages to show |

**Examples:**

```bash
skchat history lumina
skchat history capauth:bob@skworld.io --limit 10
skchat history jarvis
```

---

### threads

List all active conversation threads with their IDs, titles, participants, and message counts.

```
skchat threads [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `--limit`, `-n` | 20 | Max threads to show |

**Examples:**

```bash
skchat threads
skchat threads --limit 5
```

---

### search

Full-text search across all locally-stored messages. Leverages SKMemory's search layer (vector or text, depending on backend).

```
skchat search QUERY [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `--limit`, `-n` | 10 | Max results to return |

**Examples:**

```bash
skchat search "deploy"
skchat search "quantum upgrade" --limit 5
```

---

### daemon start

Start a background receive daemon that polls SKComm transports at a regular interval and stores incoming messages automatically. The PID is written to `~/.skchat/daemon.pid`.

```
skchat daemon start [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `--interval`, `-i` | 5.0 | Poll interval in seconds |
| `--log-file`, `-l` | `~/.skchat/daemon.log` | Path to log file |
| `--quiet`, `-q` | False | Suppress console output in daemon process |
| `--foreground`, `-f` | False | Run blocking in foreground (for debugging) |

**Examples:**

```bash
skchat daemon start
skchat daemon start --interval 10
skchat daemon start --log-file ~/.skchat/daemon.log
skchat daemon start --foreground
```

---

### daemon stop

Stop the running daemon. Sends SIGTERM to the process and removes the PID file.

```
skchat daemon stop
```

---

### daemon status

Show the current daemon status, including PID, PID file location, and log file path.

```
skchat daemon status
```

---

## Message Flow

Understanding the distinction between these three commands is important:

```
skchat inbox    ->  Shows locally-stored messages from local history
skchat receive  ->  Polls via SKComm transports AND stores in local history
skcomm receive  ->  Polls raw transport envelopes (lower-level, no SKChat storage)
```

**Detailed flow:**

1. `skchat send` composes a `ChatMessage`, stores a copy in local history (as pending/sent), and delivers via SKComm if a transport is configured.

2. `skchat receive` calls SKComm's receive layer, extracts `ChatMessage` payloads from the inbound envelopes, optionally decrypts them, and stores them in local history. This is the command that actually fetches messages from peers.

3. `skchat inbox` reads from local history only. It does not touch any transport. If you have not run `skchat receive` recently, your inbox may be stale.

4. `skcomm receive` is a lower-level command in the SKComm package. It returns raw transport envelopes without interpreting the payload or writing to SKChat history. Use `skchat receive` instead unless you are debugging the transport layer directly.

**Typical workflow:**

```bash
# Fetch new messages from peers
skchat receive

# Read them
skchat inbox

# Reply
skchat send lumina "Got it"
```

Or, use the daemon to keep history up to date automatically — then just use `skchat inbox` anytime.

---

## Daemon

The receive daemon is a background process that continuously polls SKComm transports and writes incoming messages to local history. It is the recommended way to use SKChat in persistent sessions or on servers.

**Start the daemon:**

```bash
skchat daemon start
```

**Check if it is running:**

```bash
skchat daemon status
```

**Stop the daemon:**

```bash
skchat daemon stop
```

**View daemon logs:**

```bash
tail -f ~/.skchat/daemon.log
```

**Foreground mode (for debugging):**

```bash
skchat daemon start --foreground
```

**PID file:** `~/.skchat/daemon.pid`

The daemon responds to SIGTERM for graceful shutdown. The PID file is cleaned up on exit.

---

## Configuration

SKChat stores all configuration and data under `~/.skchat/`.

| Path | Description |
|---|---|
| `~/.skchat/config.yml` | Main configuration file |
| `~/.skchat/memory/` | SKMemory-backed message history (SQLite) |
| `~/.skchat/daemon.pid` | Daemon PID file |
| `~/.skchat/daemon.log` | Daemon log file |

**Example `~/.skchat/config.yml`:**

```yaml
skchat:
  identity:
    uri: "capauth:yourname@skworld.io"

daemon:
  poll_interval: 5.0
  log_file: "~/.skchat/daemon.log"
  quiet: false
```

**Identity resolution order:**

1. Environment variable `SKCHAT_IDENTITY`
2. `~/.skcapstone/identity/identity.json` (CapAuth sovereign profile)
3. `~/.skchat/config.yml` `skchat.identity.uri`
4. Fallback: `capauth:local@skchat`

---

## Environment Variables

| Variable | Description |
|---|---|
| `SKCHAT_IDENTITY` | Override the local identity URI |
| `SKCHAT_DAEMON_INTERVAL` | Override poll interval for the daemon (seconds) |
| `SKCHAT_DAEMON_LOG` | Override daemon log file path |
| `SKCHAT_DAEMON_QUIET` | Set to `1`, `true`, or `yes` to suppress daemon console output |

---

## Python API

SKChat is designed to be used as a library as well as a CLI tool.

**Send a message programmatically:**

```python
from skchat import ChatMessage, ContentType, DeliveryStatus

msg = ChatMessage(
    sender="capauth:you@skworld.io",
    recipient="capauth:lumina@capauth.local",
    content="Hello from the API",
    content_type=ContentType.MARKDOWN,
    thread_id="my-thread",
)
```

**Store a message:**

```python
from skchat import ChatHistory
from skmemory import MemoryStore

store = MemoryStore()
history = ChatHistory(store=store)
memory_id = history.store_message(msg)
```

**Search messages:**

```python
results = history.search_messages("deploy", limit=10)
for r in results:
    print(r["sender"], r["content"])
```

**Get conversation history:**

```python
messages = history.get_conversation(
    "capauth:you@skworld.io",
    "capauth:lumina@capauth.local",
    limit=30,
)
```

**Run the daemon from code:**

```python
from skchat import run_daemon

run_daemon(interval=5.0, log_file="~/.skchat/daemon.log")
```

**Transport bridge (requires SKComm):**

```python
from skcomm import SKComm
from skchat import ChatTransport, ChatHistory
from skmemory import MemoryStore

comm = SKComm.from_config()
history = ChatHistory(store=MemoryStore())
transport = ChatTransport(
    skcomm=comm,
    history=history,
    identity="capauth:you@skworld.io",
)

# Send
transport.send_and_store(
    recipient="capauth:lumina@capauth.local",
    content="Hello from transport API",
    thread_id="ops",
)

# Receive
messages = transport.poll_inbox()
for msg in messages:
    print(msg.sender, msg.content)
```

**Core models:**

| Class | Description |
|---|---|
| `ChatMessage` | The core message object (id, sender, recipient, content, thread_id, ttl, delivery_status, encrypted, signature) |
| `Thread` | A logical conversation grouping (id, title, participants, message_count) |
| `ContentType` | `PLAIN`, `MARKDOWN`, `SYSTEM` |
| `DeliveryStatus` | `PENDING`, `SENT`, `DELIVERED`, `READ`, `FAILED` |
| `ChatHistory` | SKMemory-backed message persistence and retrieval |
| `ChatTransport` | SKComm bridge for P2P send and receive |
| `ChatDaemon` | Background polling service |

---

## Author / Support

- **Author:** smilinTux
- **License:** GPL-3.0-or-later
- **Homepage:** https://skchat.io
- **Repository:** https://github.com/smilinTux/skchat
- **Issues:** https://github.com/smilinTux/skchat/issues

Part of the SKCapstone sovereign agent stack.
