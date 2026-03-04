# SKChat Architecture

## System Overview

```mermaid
graph TB
    subgraph "SKChat"
        CLI[CLI - skchat] --> Daemon[Chat Daemon]
        MCP[MCP Server - 40 tools] --> Daemon
        TUI[Textual TUI] --> Daemon

        Daemon --> Transport[ChatTransport]
        Daemon --> History[ChatHistory - SQLite]
        Daemon --> Advocacy[AdvocacyEngine]
        Daemon --> Presence[PresenceCache]
        Daemon --> Groups[GroupChat]
    end

    subgraph "Message Flow"
        Transport --> SKComm[SKComm Layer]
        SKComm --> Syncthing[Syncthing]
        SKComm --> FileT[File Transport]
        Advocacy --> |@mention| AI[skcapstone AI Response]
    end

    subgraph "Security"
        Crypto[PGP Crypto] --> Transport
        Plugins[SKSeal Plugin] --> Groups
        EStore[Encrypted Store] --> History
    end

    subgraph "Identity"
        PeerDisc[Peer Discovery] --> Daemon
        IDbridge[Identity Bridge] --> Transport
        DID[DID Publishing] --> |Tier 3| CF[Cloudflare KV]
    end
```

## Component Descriptions

### SKChat Layer

| Component | Module | Description |
|-----------|--------|-------------|
| CLI | `cli.py` | Click-based `skchat` command |
| MCP Server | `mcp_server.py` | FastMCP server exposing 40 tools to AI agents |
| Textual TUI | `tui.py` | Terminal UI (`skchat-tui` entry point) |
| Chat Daemon | `daemon.py` | Core polling loop; manages advocacy engine and WebRTC |
| ChatTransport | `transport.py` | Send/receive over SKComm |
| ChatHistory | `history.py` | Persistent SQLite message store |
| AdvocacyEngine | `advocacy.py` | Detects `@mention`, calls skcapstone for AI responses |
| PresenceCache | `presence.py` | Online/offline tracking |
| GroupChat | `group.py` | Encrypted group messaging |

### Message Flow Layer

| Component | Description |
|-----------|-------------|
| SKComm Layer | P2P transport — 17 redundant paths (Netbird, Tailscale, Tor, Nostr, etc.) |
| Syncthing | File sync transport backend |
| File Transport | Direct file-based message delivery |
| skcapstone AI | AI response generation triggered by `@mention` |

### Security Layer

| Component | Module | Description |
|-----------|--------|-------------|
| PGP Crypto | `crypto.py` | PGP sign/verify helpers (PGPy) |
| SKSeal Plugin | `plugins_skseal.py` | Encryption plugin for group messages |
| Encrypted Store | `encrypted_store.py` | AES-encrypted local storage |

### Identity Layer

| Component | Module | Description |
|-----------|--------|-------------|
| Peer Discovery | `peer_discovery.py` | Loads peers from `~/.skcapstone/peers/` |
| Identity Bridge | `identity_bridge.py` | Resolves CapAuth identities to SKComm addresses |
| DID Publishing | `scripts/publish-did.sh` | Publishes DID to Cloudflare KV for Tier 3 resolution |

## Installation

All SK* packages install into a shared virtual environment at `~/.skenv/`:

```bash
# Via SK* suite installer
bash path/to/skcapstone/scripts/install.sh

# Or standalone
python3 -m venv ~/.skenv
~/.skenv/bin/pip install skchat-sovereign
export PATH="$HOME/.skenv/bin:$PATH"
```

## Data Flow — Outbound Message

```
User/Agent
    │
    ▼
CLI / MCP Tool / TUI
    │
    ▼
ChatDaemon (daemon.py)
    │
    ├─► AdvocacyEngine — privacy screening, @mention routing
    │
    ▼
ChatTransport (transport.py)
    │  PGP encrypt
    ▼
SKComm Layer
    │  17 redundant transports
    ▼
Peer / Group
```

## Data Flow — Inbound Message

```
SKComm Layer
    │  receive + PGP decrypt
    ▼
ChatTransport (transport.py)
    │
    ▼
ChatDaemon polling loop
    │
    ├─► ChatHistory (SQLite) — persist
    ├─► PresenceCache — update online status
    ├─► AdvocacyEngine — @mention check → skcapstone response
    │
    ▼
Inbox / TUI / MCP check_inbox tool
```
