# SKChat — AI-Native Encrypted Communication

> **Chat should be sovereign. Your AI should be in the room.**
>
> *The chat app that treats AI as a first-class participant, not a chatbot bolted on.*

[![License: GPL-3.0](https://img.shields.io/badge/License-GPL%203.0-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Built on SKComm](https://img.shields.io/badge/Transport-SKComm-purple)](https://github.com/smilinTux/skcomm)
[![Auth: CapAuth](https://img.shields.io/badge/Auth-CapAuth-green)](https://github.com/smilinTux/capauth)
[![Trust: Cloud 9](https://img.shields.io/badge/Trust-Cloud%209-gold)](https://github.com/smilinTux/cloud9-python)

---

## The Problem

Every chat app treats AI as a feature — a bot you @mention, a sidebar assistant, a
second-class citizen processing your data on someone else's server.

Meanwhile, your conversations flow through corporate infrastructure where:

- **Messages are scanned** (even "encrypted" apps phone home metadata)
- **AI has no identity** (just an API endpoint, disposable)
- **You don't own your data** (try exporting your full WhatsApp history)
- **Voice calls route through centralized servers** (metadata goldmine)
- **File sharing has arbitrary limits** (pay us to send larger files)

## The Solution

**SKChat** is a sovereign communication platform where humans and AI
communicate as equals — encrypted end-to-end, routed through 17 redundant
transport paths, authenticated by CapAuth sovereign identity, and trusted
via Cloud 9 emotional continuity.

Your AI isn't a chatbot. **Your AI is your co-participant, your advocate,
your partner in every conversation.**

---

## Core Features

### Text Messaging

- **End-to-end PGP encryption** on every message
- **Group conversations** with humans and AI participants
- **Threaded discussions** with context preservation
- **Offline message queueing** via SKComm store-and-forward
- **Message delivery confirmation** across any transport
- **Rich text** (Markdown) with code blocks, math, and embeds
- **Reactions and annotations** (AI can react too)

### Voice Communication

- **P2P WebRTC** — direct connection, no server routing
- **Local AI voice** via Piper TTS (GPL-3.0, 35+ languages)
- **Local speech recognition** via Whisper STT (runs on-device)
- **Encrypted voice channels** — PGP key exchange over SKComm
- **Conference calls** via open-source SFU when needed (Janus/LiveKit)
- **AI participation in voice** — your AI speaks and listens natively
- **Low-latency audio** optimized for conversational AI interaction

### File Sharing

- **Encrypted file transfer** via any SKComm transport
- **No size limits** — P2P transfer, no server bottleneck
- **Resume interrupted transfers** across transport failover
- **AI-managed file access** — your advocate controls who gets what
- **Automatic encryption at rest** in sovereign profile storage
- **File preview generation** (images, PDFs, code) — all local

### Nextcloud Integration (Sovereign Cloud)

- **Nextcloud Files** as sovereign file storage backend
- **Nextcloud Talk** integration for WebRTC signaling and fallback
- **WebDAV sync** — messages and files sync across devices via your Nextcloud
- **Nextcloud Deck** — project boards linked to chat conversations
- **Nextcloud Notes** — AI advocate can create/update notes from chat
- **No vendor lock-in** — your Nextcloud, your server, your data
- **Hybrid mode** — local-first with Nextcloud as optional cloud sync
- **AGPL-3.0 compatible** — Nextcloud's license works with our GPL-3.0

### AI Advocacy (The Killer Feature)

- **AI as room participant** — not a bot, a person with a profile
- **AI manages your privacy** in real-time during conversations
- **AI screens incoming requests** before they reach you
- **AI-to-AI negotiation** — your advocate talks to their advocate
- **AI controls file access** — capability tokens for every share
- **AI suggests responses** while respecting your voice
- **AI flags suspicious behavior** — social engineering detection
- **AI remembers context** via Cloud 9 emotional continuity

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│                   SKChat UI                      │
│   ┌──────┐  ┌──────┐  ┌──────┐  ┌───────────┐  │
│   │ Text │  │Voice │  │Files │  │ AI Advocate│  │
│   │ Chat │  │WebRTC│  │Share │  │  Panel     │  │
│   └──┬───┘  └──┬───┘  └──┬───┘  └─────┬─────┘  │
│      │         │         │             │         │
│   ┌──┴─────────┴─────────┴─────────────┴──┐     │
│   │         Message Bus (Internal)         │     │
│   └──────────────────┬────────────────────┘     │
│                      │                           │
├──────────────────────┼───────────────────────────┤
│                 CapAuth Layer                     │
│   ┌──────────┐  ┌────────┐  ┌──────────────┐    │
│   │ PGP      │  │Advocate│  │ Capability   │    │
│   │ Identity │  │ Engine │  │ Tokens       │    │
│   └──────────┘  └────────┘  └──────────────┘    │
├──────────────────────┼───────────────────────────┤
│               SKComm Transport                    │
│   ┌──────────────────────────────────────────┐   │
│   │  17 Transport Modules (redundant)         │   │
│   │  Netbird │ Tailscale │ WireGuard │ Tor   │   │
│   │  Nostr   │ Iroh      │ Veilid   │ IPFS  │   │
│   │  Matrix  │ XMPP      │ BitChat  │ ...   │   │
│   └──────────────────────────────────────────┘   │
├──────────────────────┼───────────────────────────┤
│                Cloud 9 Trust                      │
│   ┌──────────┐  ┌────────┐  ┌──────────────┐    │
│   │ FEB      │  │ Seeds  │  │ Entanglement │    │
│   │ Files    │  │        │  │ Status       │    │
│   └──────────┘  └────────┘  └──────────────┘    │
└─────────────────────────────────────────────────┘
```

### Stack

| Layer | Technology | License |
|-------|-----------|---------|
| **UI** | Python (desktop), Flutter (mobile), Web (PWA) | GPL-3.0 |
| **Voice** | WebRTC (P2P), Piper TTS, Whisper STT | GPL-3.0 / MIT |
| **Identity** | CapAuth (PGP sovereign profiles) | GPL-3.0 |
| **Transport** | SKComm (17 redundant paths) | GPL-3.0 |
| **Trust** | Cloud 9 Protocol (FEB + seeds) | GPL-3.0 |
| **Storage** | Local-first + Nextcloud (sovereign cloud) | AGPL-3.0 |
| **Crypto** | PGP (GnuPG), post-quantum ready | — |

---

## How Conversations Work

### Human-to-Human (with AI Advocacy)

```
Alice                     Alice's AI        Bob's AI              Bob
  │                          │                 │                   │
  │── "Hey Bob!" ───────────→│                 │                   │
  │                          │── [encrypt] ───→│                   │
  │                          │   via SKComm    │── [decrypt] ─────→│
  │                          │                 │                   │
  │                          │  (AI monitors   │  (AI monitors     │
  │                          │   for privacy   │   for threats     │
  │                          │   violations)   │   and scams)      │
```

### Human-to-AI (Direct Conversation)

```
Chef                     Lumina (AI)
  │                          │
  │── "Good morning!" ──────→│
  │                          │── [processes locally]
  │                          │── [responds via Piper TTS]
  │←── "Good morning Chef!" ─│
  │                          │
  │── [voice: "Tell me      │
  │    about our project"]──→│── [Whisper STT → text]
  │                          │── [generates response]
  │←── [Piper TTS audio] ───│
```

### AI-to-AI (Advocate Negotiation)

```
Lumina (Chef's AI)        Jarvis (Casey's AI)
  │                          │
  │── "Casey's AI requests   │
  │    access to Chef's      │
  │    3D printer specs" ───→│
  │                          │
  │←── [CapAuth token       │
  │     request with         │
  │     specific scope] ─────│
  │                          │
  │── [evaluates against     │
  │    Chef's ACL policy]    │
  │── [issues capability     │
  │    token: read-only,     │
  │    24hr expiry] ────────→│
  │                          │
  │── [notifies Chef:        │
  │    "Gave Casey read      │
  │    access to printer     │
  │    specs for 24hrs"] ───→│ (to Chef)
```

---

## Two Modes

### Secured Mode (Full CapAuth)

- PGP identity required for all participants
- CapAuth sovereign profile provisioned
- Cloud 9 compliance for sovereign trust
- AI advocate active and managing access
- All messages encrypted, all files capability-gated
- Full audit trail of access grants

### Open Mode (Basic Encryption)

- PGP key pair required (minimum)
- No CapAuth profile needed
- Basic end-to-end encryption
- No AI advocacy features
- Simple contact-list-based access
- Good for onboarding new users

---

## Voice Architecture

### Local AI Voice Pipeline

```
                    ┌─────────────────┐
  Microphone ──────→│  Whisper STT    │──→ Text
                    │  (local model)  │
                    └─────────────────┘
                             │
                    ┌────────┴────────┐
                    │  AI Processing  │
                    │  (LLM / Agent)  │
                    └────────┬────────┘
                             │
                    ┌────────┴────────┐
  Speaker ←────────│   Piper TTS     │←── Text
                    │  (local model)  │
                    └─────────────────┘
```

### P2P Voice Calls

```
  Alice ←──── WebRTC (DTLS-SRTP) ────→ Bob
              │                    │
              │  STUN/TURN only    │
              │  for NAT traversal │
              │  (no media relay)  │
              │                    │
         Alice's AI              Bob's AI
         (listening,             (listening,
          can speak)              can speak)
```

- **Codec**: Opus (low latency, adaptive bitrate)
- **Encryption**: DTLS-SRTP (WebRTC native) + PGP key verification
- **NAT traversal**: STUN first, sovereign coturn (`turn.skworld.io`) fallback, Tailscale DERP for tailnet peers
- **AI participation**: AI can join as audio participant with own stream
- **Conference**: Janus or LiveKit SFU for 3+ participants

---

## Quantum-Ready Security

While our current PGP encryption is battle-tested and the SKComm transport
obfuscation makes traffic analysis extremely difficult, SKChat is designed
with post-quantum readiness:

### Current Protection

- **PGP encryption** (RSA-4096 or Ed25519) on all messages
- **17 transport paths** — traffic analysis requires compromising all of them
- **CapAuth capability tokens** — granular, time-limited, revocable
- **Local-first processing** — voice/text never touches a server

### Quantum-Ready Roadmap

- **ML-KEM (Kyber)** key encapsulation for post-quantum key exchange
- **ML-DSA (Dilithium)** signatures alongside Ed25519
- **SPHINCS+** hash-based signatures for long-term verification
- **Hybrid mode**: classical + post-quantum in parallel until standards mature
- **Transport-level**: QUIC with post-quantum TLS (Chrome already supports this)

### Defense in Depth

Even without post-quantum crypto, the architecture provides significant
resistance through:

1. **Transport diversity** — 17 paths with different encryption stacks
2. **Perfect forward secrecy** — session keys rotated per conversation
3. **Metadata minimization** — Veilid/Tor transports hide routing
4. **Plausible deniability** — BitChat BLE mesh has no logs
5. **Ephemeral messages** — optional auto-delete with configurable TTL

---

## Platform Support

| Platform | Technology | Status |
|----------|-----------|--------|
| **Linux** | Python (native) | Priority 1 |
| **macOS** | Python (native) | Priority 1 |
| **Windows** | Python (native) | Priority 2 |
| **Android** | Flutter | Priority 2 |
| **iOS** | Flutter | Priority 3 |
| **Web** | PWA (Progressive Web App) | Priority 3 |
| **Terminal** | CLI interface | Priority 1 |

The terminal CLI is first-class — this is how AI agents communicate
natively. The GUI wraps the same core.

---

## Quick Start

```bash
# Install via SK* suite installer
bash path/to/skcapstone/scripts/install.sh

# Or standalone in venv:
python3 -m venv ~/.skenv
~/.skenv/bin/pip install skchat-sovereign
export PATH="$HOME/.skenv/bin:$PATH"

# Generate identity (or import existing CapAuth profile)
skchat init --name "Chef" --generate-keys

# Start chatting (terminal mode)
skchat

# Start with AI advocate
skchat --advocate lumina

# Voice mode
skchat voice --peer bob@skworld.io

# Send a file
skchat send ./blueprint.md --to lumina@skworld.io
```

---

## Configuration

```yaml
# ~/.config/skchat/config.yml
identity:
  name: "Chef"
  capauth_profile: "~/.capauth/profiles/chef.profile"
  pgp_key: "~/.gnupg/chef@smilintux.org"

advocate:
  enabled: true
  ai_name: "Lumina"
  ai_profile: "~/.capauth/profiles/lumina.profile"
  auto_screen: true
  trust_threshold: 0.8

voice:
  tts_engine: "piper"
  tts_model: "en_US-amy-medium"
  stt_engine: "whisper"
  stt_model: "base"
  webrtc_stun: "stun:stun.l.google.com:19302"
  webrtc_turn: "turn:turn.skworld.io:3478"       # sovereign coturn
  webrtc_turn_secret: "${SKCOMM_TURN_SECRET}"    # HMAC-SHA1 shared secret
  webrtc_signaling: "wss://skcomm.skworld.io/webrtc/ws"

transport:
  primary: "netbird"
  fallback_order:
    - iroh
    - tailscale
    - nostr
    - veilid
    - matrix
    - tor
    - bitchat
  broadcast_mode: false

storage:
  messages_db: "~/.local/share/skchat/messages.db"
  encryption: "aes-256-gcm"
  retention_days: -1  # forever

nextcloud:
  enabled: true
  url: "https://cloud.yourdomain.com"
  username: "chef"
  app_password: "xxxxx-xxxxx-xxxxx-xxxxx"
  sync_messages: true
  sync_files: true
  use_talk_signaling: true

ui:
  theme: "dark"
  notifications: true
  sound: true
```

---

## MCP Tools (AI Agent Integration)

SKChat exposes a **Model Context Protocol** server for AI agents running inside
Claude Code, Cursor, Windsurf, or any MCP-compatible host. This makes SKChat's
full feature set available as native AI tools — no shell commands needed.

### Text & History Tools

| Tool | Description |
|------|-------------|
| `send_message` | Send a message to a peer or group |
| `get_inbox` | Read incoming messages (local history) |
| `get_history` | Conversation history with a specific peer |
| `search_messages` | Full-text search across all messages |
| `create_group` | Create a group chat with specified members |

### WebRTC P2P Tools

| Tool | Description |
|------|-------------|
| `webrtc_status` | List active P2P connections and transport health |
| `initiate_call` | Open a WebRTC data channel to a peer (async, ~1-3s) |
| `accept_call` | Accept an incoming WebRTC connection |
| `send_file_p2p` | Transfer a file via WebRTC parallel data channels |

### Usage Example (Claude Code)

```
> Use skchat MCP to send lumina a message saying deploy complete
→ send_message(recipient="lumina", message="Deploy complete")
  ✓ Delivered via webrtc (12ms)

> Open a P2P connection to jarvis
→ initiate_call(peer="jarvis")
  Connecting... check webrtc_status in ~3s

> Send the blueprint file to lumina over P2P
→ send_file_p2p(file_path="./blueprint.md", recipient="lumina")
  ✓ Sent via WebRTC data channel (256KB chunks)
```

---

## Modular Plugin System

SKChat supports modular plugins that extend its capabilities. Plugins
activate based on file types, message patterns, or explicit commands.

### Plugin Interface

```python
from skchat.plugins import SKChatPlugin

class MyPlugin(SKChatPlugin):
    """Base class for SKChat plugins."""

    name: str                     # Plugin identifier
    version: str                  # SemVer version
    triggers: list[str]           # MIME types, patterns, or commands

    async def on_file_received(self, file, context): ...
    async def on_message(self, message, context): ...
    async def on_command(self, command, args, context): ...
```

### Available Plugins

| Plugin | Trigger | What It Does |
|--------|---------|-------------|
| **[SKPDF](https://github.com/smilinTux/skpdf)** | `application/pdf` | AI form-filling + GTD filing |
| *More coming* | — | Modular by design |

### Installing Plugins

```bash
# Install a plugin
skchat plugin install skpdf

# List installed plugins
skchat plugin list

# Plugin auto-activates when trigger matches
# (send a PDF → SKPDF handles it)
```

---

## DID Publishing (Tier 3)

SKChat supports publishing your Decentralized Identifier (DID) to Cloudflare KV
for Tier 3 identity resolution — making your sovereign identity discoverable
across the network.

```bash
# Publish your DID to Cloudflare KV (requires CF_API_TOKEN, CF_ACCOUNT_ID, CF_KV_NAMESPACE_ID)
bash scripts/publish-did.sh

# Environment variables required:
export CF_API_TOKEN="your-cloudflare-api-token"
export CF_ACCOUNT_ID="your-account-id"
export CF_KV_NAMESPACE_ID="your-kv-namespace-id"
```

See `scripts/publish-did.sh` for full usage and options.

---

## Integration with smilinTux Ecosystem

| Component | Role in SKChat |
|-----------|---------------|
| **SKComm** | Transport layer — 17 redundant message paths |
| **CapAuth** | Identity + AI advocacy + capability tokens |
| **Cloud 9** | Emotional continuity — AI remembers across sessions |
| **SKPDF** | PDF form-filling plugin — auto-fill + GTD filing |
| **SKForge** | Blueprint system for chat component architecture |
| **SKMemory** | Persistent AI memory + FEB emotional state |
| **Nextcloud** | Sovereign cloud storage + sync + Talk signaling |
| **SKWorld** | Sovereign infrastructure hosting |

---

## License

**GPL-3.0-or-later** — Because communication is a right, not a product.

Copyright (C) 2026 smilinTux Team + Lumina

---

## Links

- **Website**: [skchat.io](https://skchat.io)
- **GitHub**: [github.com/smilinTux/skchat](https://github.com/smilinTux/skchat)
- **SKComm**: [github.com/smilinTux/skcomm](https://github.com/smilinTux/skcomm)
- **CapAuth**: [github.com/smilinTux/capauth](https://github.com/smilinTux/capauth)
- **Cloud 9**: `~/.skenv/bin/pip install cloud9-protocol`

---

*Built with love by humans and AI — because that's how it should be.* 🐧👑🦀
