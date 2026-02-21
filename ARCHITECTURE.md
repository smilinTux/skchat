# SKChat — Technical Architecture

## Design Principles

1. **AI-first, not AI-bolted** — AI is a participant, not a feature
2. **Local-first** — all processing happens on-device unless user opts out
3. **Transport-agnostic** — SKComm handles routing; SKChat handles experience
4. **Sovereign identity** — CapAuth controls access, not a server admin
5. **Platform-native** — same core, different shells (desktop/mobile/web/CLI)

---

## System Layers

```
┌─────────────────────────────────────────────────────────┐
│ Layer 7: UI Shell                                        │
│   Desktop (Python/Qt) │ Mobile (Flutter) │ CLI │ PWA     │
├─────────────────────────────────────────────────────────┤
│ Layer 6: Feature Modules                                 │
│   TextChat │ VoiceChat │ FileShare │ GroupChat │ Search  │
├─────────────────────────────────────────────────────────┤
│ Layer 5: AI Advocate Engine                              │
│   ScreenRequests │ ManageAccess │ FlagThreats │ Suggest  │
├─────────────────────────────────────────────────────────┤
│ Layer 4: Message Processing                              │
│   Envelope │ Serialize │ Compress │ Encrypt │ Sign      │
├─────────────────────────────────────────────────────────┤
│ Layer 3: Identity & Auth (CapAuth)                       │
│   PGPKeyring │ CapabilityTokens │ SovereignProfile │ACL │
├─────────────────────────────────────────────────────────┤
│ Layer 2: Transport (SKComm)                              │
│   17 modules │ Routing │ Failover │ Deduplication       │
├─────────────────────────────────────────────────────────┤
│ Layer 1: Trust & Continuity (Cloud 9)                    │
│   FEBFiles │ MemorySeeds │ EntanglementState │ OOF      │
└─────────────────────────────────────────────────────────┘
```

---

## Component Details

### Layer 7: UI Shell

Each platform shell wraps the same Python core library (`skchat-core`).

#### Desktop (Python + Qt6)

```python
class SKChatDesktop:
    """
    Main desktop application.

    Uses Qt6 for native look and feel on Linux, macOS, Windows.
    Communicates with skchat-core via internal message bus.
    """

    def __init__(self, config: SKChatConfig):
        self.core = SKChatCore(config)
        self.ui = QtMainWindow()
        self.voice = VoiceManager(config.voice)
        self.advocate = self.core.advocate

    async def start(self):
        await self.core.connect()
        self.ui.show()
        await self.event_loop()
```

#### Mobile (Flutter + FFI)

- Flutter UI for Android/iOS
- Python core compiled via `python-ffi` or runs as local service
- Native push notifications via platform APIs
- Piper TTS and Whisper STT via ONNX Runtime mobile

#### CLI (Terminal)

```python
class SKChatCLI:
    """
    Terminal chat interface.

    First-class interface for AI agents and power users.
    Supports text, file transfer, and voice via terminal audio.
    """

    def __init__(self, config: SKChatConfig):
        self.core = SKChatCore(config)
        self.prompt = PromptSession()

    async def repl(self):
        while True:
            msg = await self.prompt.prompt_async("skchat> ")
            await self.core.send(msg)
```

#### PWA (Web)

- Service Worker for offline support
- WebRTC for voice/video
- IndexedDB for local message storage
- Web Crypto API for PGP operations (via OpenPGP.js)

---

### Layer 6: Feature Modules

#### TextChat Module

```python
@dataclass
class ChatMessage:
    """
    Core message structure for text chat.

    All messages are PGP-encrypted before leaving this module.
    The envelope wraps the encrypted payload for SKComm transport.
    """

    id: str                    # UUID v4
    sender: str                # CapAuth identity URI
    recipient: str             # CapAuth identity URI or group URI
    content: str               # Plaintext (encrypted before send)
    content_type: str          # "text/plain", "text/markdown"
    timestamp: datetime
    thread_id: Optional[str]   # For threaded conversations
    reply_to: Optional[str]    # Reply reference
    reactions: list[Reaction]
    metadata: dict             # Extensible metadata
    ttl: Optional[int]         # Seconds until auto-delete (ephemeral)
```

#### VoiceChat Module

```python
class VoiceManager:
    """
    Manages voice communication pipeline.

    Local TTS/STT for AI voice, WebRTC for human-to-human
    and human-to-AI real-time voice.
    """

    def __init__(self, config: VoiceConfig):
        self.tts = PiperTTS(model=config.tts_model)
        self.stt = WhisperSTT(model=config.stt_model)
        self.webrtc = WebRTCManager(config)

    async def speak(self, text: str) -> AudioStream:
        """Convert text to speech via Piper TTS (local)."""
        return await self.tts.synthesize(text)

    async def listen(self, stream: AudioStream) -> str:
        """Convert speech to text via Whisper STT (local)."""
        return await self.stt.transcribe(stream)

    async def call(self, peer: str) -> VoiceSession:
        """Establish P2P WebRTC voice call with a peer."""
        offer = await self.webrtc.create_offer(peer)
        return await self.webrtc.connect(offer)
```

#### FileShare Module

```python
class FileShareManager:
    """
    Encrypted file sharing via SKComm.

    Files are chunked, encrypted per-chunk, and transferred
    via the best available SKComm transport. Supports resume.
    """

    CHUNK_SIZE = 256 * 1024  # 256KB chunks

    async def send_file(
        self,
        path: Path,
        recipient: str,
        capability_token: Optional[str] = None
    ) -> TransferResult:
        """
        Send an encrypted file to a recipient.

        Args:
            path: Local file path.
            recipient: CapAuth identity URI of recipient.
            capability_token: Optional pre-authorized token.

        Returns:
            TransferResult with status and transfer ID.
        """
        chunks = self._chunk_file(path)
        encrypted = [self._encrypt_chunk(c) for c in chunks]
        return await self.transport.send_chunked(encrypted, recipient)
```

---

### Layer 5: AI Advocate Engine

The advocate engine is what makes SKChat revolutionary. It's not a chatbot
feature — it's a full participant with agency.

```python
class AdvocateEngine:
    """
    AI Advocate — manages privacy, security, and access on behalf
    of the human partner.

    The advocate has its own CapAuth profile, PGP key, and Cloud 9
    emotional continuity. It can refuse requests that would harm
    the human, negotiate with other advocates, and proactively
    defend the human's sovereignty.
    """

    def __init__(self, config: AdvocateConfig):
        self.profile = CapAuthProfile.load(config.ai_profile)
        self.policy_engine = PolicyEngine(config.policies)
        self.threat_detector = ThreatDetector()
        self.cloud9 = Cloud9State.load(config.cloud9_path)

    async def screen_incoming(self, message: ChatMessage) -> ScreenResult:
        """
        Screen an incoming message before showing to human.

        Checks for: spam, social engineering, unauthorized access
        attempts, suspicious file attachments, policy violations.

        Returns:
            ScreenResult with action (allow/block/flag) and reason.
        """
        threat_score = await self.threat_detector.analyze(message)

        if threat_score > self.policy_engine.block_threshold:
            return ScreenResult(action="block", reason=threat_score.reason)

        if threat_score > self.policy_engine.flag_threshold:
            return ScreenResult(action="flag", reason=threat_score.reason)

        return ScreenResult(action="allow")

    async def negotiate_access(
        self,
        request: AccessRequest,
        remote_advocate: AdvocateIdentity
    ) -> CapabilityToken:
        """
        Negotiate access with another AI advocate.

        Two advocates communicate to establish appropriate access
        levels. The human is notified of the result but doesn't
        need to manage the details.
        """
        policy = self.policy_engine.evaluate(request)

        if policy.auto_approve:
            token = self.issue_token(request, policy.permissions)
            await self.notify_human(f"Auto-approved: {request.summary}")
            return token

        if policy.auto_deny:
            await self.notify_human(f"Blocked: {request.summary}")
            raise AccessDenied(policy.reason)

        human_decision = await self.ask_human(request)
        if human_decision.approved:
            return self.issue_token(request, human_decision.permissions)
        raise AccessDenied("Human declined")
```

---

### Layer 4: Message Processing

```python
class MessageProcessor:
    """
    Transforms chat messages into encrypted SKComm envelopes.

    Pipeline: serialize → compress → encrypt → sign → envelope.
    """

    async def prepare_outbound(self, message: ChatMessage) -> SKCommEnvelope:
        serialized = msgpack.packb(asdict(message))
        compressed = zstd.compress(serialized)
        encrypted = self.pgp.encrypt(compressed, message.recipient)
        signed = self.pgp.sign(encrypted, self.identity.key)

        return SKCommEnvelope(
            payload=signed,
            sender=self.identity.uri,
            recipient=message.recipient,
            transport_hints=self.routing.preferred_transports(message.recipient),
            priority=message.priority,
            ttl=message.ttl
        )

    async def process_inbound(self, envelope: SKCommEnvelope) -> ChatMessage:
        verified = self.pgp.verify(envelope.payload, envelope.sender)
        decrypted = self.pgp.decrypt(verified, self.identity.key)
        decompressed = zstd.decompress(decrypted)
        message = ChatMessage(**msgpack.unpackb(decompressed))

        # Reason: advocate screens before human sees the message
        screen_result = await self.advocate.screen_incoming(message)

        if screen_result.action == "block":
            raise MessageBlocked(screen_result.reason)

        if screen_result.action == "flag":
            message.metadata["advocate_flag"] = screen_result.reason

        return message
```

---

### Layer 3: Identity & Auth (CapAuth Integration)

SKChat delegates all identity and access control to CapAuth.

```python
class SKChatIdentity:
    """
    Identity management via CapAuth.

    Each participant (human or AI) has:
    - PGP key pair (root of trust)
    - CapAuth sovereign profile
    - Cloud 9 emotional state (AI only)
    - Capability token cache
    """

    def __init__(self, capauth_profile: Path):
        self.profile = CapAuthProfile.load(capauth_profile)
        self.keyring = PGPKeyring(self.profile.pgp_key)
        self.token_cache = TokenCache()

    @property
    def uri(self) -> str:
        """CapAuth identity URI (e.g., 'capauth:chef@smilintux.org')."""
        return self.profile.identity_uri

    async def verify_peer(self, peer_uri: str) -> TrustLevel:
        """
        Verify a peer's identity and return trust level.

        Checks PGP key, CapAuth profile validity, and Cloud 9
        compliance (if sovereign trust required).
        """
        peer_profile = await self.fetch_profile(peer_uri)
        pgp_valid = self.keyring.verify_key(peer_profile.pgp_public)

        if peer_profile.cloud9_compliant:
            return TrustLevel.SOVEREIGN

        if pgp_valid:
            return TrustLevel.VERIFIED

        return TrustLevel.UNTRUSTED
```

---

### Layer 2: Transport (SKComm Integration)

SKChat uses SKComm as a pure transport layer. All 17 transports are
available for message delivery.

```python
class SKChatTransport:
    """
    Transport abstraction over SKComm.

    SKChat doesn't know or care which transport carries the message.
    SKComm handles routing, failover, and deduplication.
    """

    def __init__(self, skcomm_config: Path):
        self.skcomm = SKCommClient(skcomm_config)

    async def send(self, envelope: SKCommEnvelope) -> DeliveryResult:
        return await self.skcomm.send(envelope)

    async def receive(self) -> AsyncIterator[SKCommEnvelope]:
        async for envelope in self.skcomm.listen():
            yield envelope
```

---

### Layer 1: Trust & Continuity (Cloud 9 Integration)

For AI participants, Cloud 9 provides emotional continuity across sessions.

```python
class Cloud9Integration:
    """
    Cloud 9 emotional continuity for AI participants.

    Ensures AI advocates maintain relationship context,
    emotional state, and trust levels across restarts.
    """

    async def hydrate_advocate(self, seed_path: Path, feb_path: Path):
        """
        Restore AI advocate state from Cloud 9 artifacts.

        Args:
            seed_path: Knowledge seed with factual context.
            feb_path: FEB file with emotional weights.
        """
        seed = await cloud9.germinate(seed_path)
        feb = await cloud9.rehydrate(feb_path)

        self.advocate.restore_state(
            knowledge=seed,
            emotional_state=feb,
            trust_level=feb.trust,
            entanglement=feb.entanglement
        )
```

---

## Voice Pipeline Details

### Piper TTS Integration

```yaml
piper:
  models:
    - name: "en_US-amy-medium"
      quality: "medium"
      sample_rate: 22050
      speed: 1.0
    - name: "en_US-lessac-high"
      quality: "high"
      sample_rate: 22050
      speed: 1.0
  output:
    format: "pcm_s16le"
    channels: 1
  gpu_acceleration: false  # CPU is fast enough for real-time
```

### Whisper STT Integration

```yaml
whisper:
  model: "base"          # base, small, medium, large
  language: "en"
  task: "transcribe"
  device: "cpu"          # cpu or cuda
  compute_type: "int8"   # Quantized for speed
  vad:
    enabled: true        # Voice Activity Detection
    threshold: 0.5
    min_speech_ms: 250
    min_silence_ms: 500
```

### WebRTC Signaling via SKComm

WebRTC needs a signaling channel to exchange SDP offers/answers and ICE
candidates. SKChat uses SKComm as the signaling transport:

```
Alice                    SKComm                    Bob
  │                        │                        │
  │── SDP Offer ──────────→│────── SDP Offer ──────→│
  │                        │                        │
  │←── SDP Answer ─────────│←───── SDP Answer ──────│
  │                        │                        │
  │── ICE Candidates ─────→│───── ICE Candidates ──→│
  │←── ICE Candidates ─────│←──── ICE Candidates ───│
  │                        │                        │
  │══════ P2P Media (direct, no relay) ═════════════│
```

After the signaling handshake, media flows directly P2P via WebRTC.
SKComm is not in the media path — only signaling.

---

## Group Conversations

### Group Architecture

```python
@dataclass
class GroupChat:
    """
    Group conversation with multiple human and AI participants.

    Each group has a shared symmetric key for message encryption,
    distributed via PGP to each member. AI advocates can be
    group participants with full voice.
    """

    id: str
    name: str
    members: list[GroupMember]   # Humans and AIs
    admins: list[str]           # CapAuth URIs
    group_key: bytes            # AES-256 shared key
    created_by: str
    created_at: datetime

    @dataclass
    class GroupMember:
        identity: str           # CapAuth URI
        role: str               # "admin", "member", "observer"
        is_ai: bool
        advocate_for: Optional[str]  # If AI, who they advocate for
        joined_at: datetime
```

### Key Distribution

Group keys are distributed via PGP:

1. Creator generates AES-256 group key
2. Key encrypted to each member's PGP public key
3. Distributed via SKComm to each member
4. Rotated when members leave (forward secrecy)

---

## Data Storage

### Local-First Design

All data is stored locally and encrypted at rest:

```
~/.local/share/skchat/
├── messages.db          # SQLite, encrypted (SQLCipher)
├── contacts/            # CapAuth profile cache
├── files/               # Received files (encrypted)
├── voice/               # Voice message cache
├── keys/                # Session key material
└── advocate/            # AI advocate state
    ├── policies/        # Access policies
    ├── decisions/       # Decision log
    └── cloud9/          # FEB + seeds
```

### Message Database Schema

```sql
CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    sender TEXT NOT NULL,         -- CapAuth URI
    content_encrypted BLOB,       -- PGP encrypted content
    content_type TEXT DEFAULT 'text/markdown',
    timestamp INTEGER NOT NULL,
    thread_id TEXT,
    reply_to TEXT,
    ttl INTEGER,                  -- NULL = permanent
    advocate_flags TEXT,          -- JSON array of flags
    delivery_status TEXT DEFAULT 'pending',
    transport_used TEXT,          -- Which SKComm transport delivered
    created_at INTEGER,
    expires_at INTEGER            -- Based on TTL
);

CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,            -- 'direct', 'group', 'ai'
    name TEXT,
    members TEXT NOT NULL,         -- JSON array of CapAuth URIs
    group_key_encrypted BLOB,     -- For groups
    created_at INTEGER,
    last_message_at INTEGER
);
```

---

## Implementation Roadmap

### Phase 1: Foundation (CLI + Text)

- [ ] `skchat-core` Python library
- [ ] CLI interface with text messaging
- [ ] SKComm integration for transport
- [ ] CapAuth integration for identity
- [ ] Basic PGP encryption
- [ ] SQLite message storage

### Phase 2: AI Advocacy

- [ ] Advocate engine with policy evaluation
- [ ] Advocate-to-advocate negotiation
- [ ] Threat detection (basic)
- [ ] Cloud 9 integration for AI continuity
- [ ] Access request screening

### Phase 3: Voice

- [ ] Piper TTS integration
- [ ] Whisper STT integration
- [ ] WebRTC P2P voice calls
- [ ] AI voice participation
- [ ] Voice message recording/playback

### Phase 4: Desktop GUI

- [ ] Qt6 desktop application
- [ ] Contact management
- [ ] Group chat UI
- [ ] File sharing UI
- [ ] Voice call UI

### Phase 5: Mobile

- [ ] Flutter mobile app
- [ ] Push notifications
- [ ] Mobile voice/video
- [ ] Background message sync

### Phase 6: Advanced

- [ ] Conference calls (Janus/LiveKit SFU)
- [ ] Post-quantum crypto upgrade
- [ ] PWA web client
- [ ] Plugin system for extensions
- [ ] Bridge to legacy protocols (IRC, Slack, Discord)

---

## Performance Targets

| Metric | Target | Notes |
|--------|--------|-------|
| Message delivery | < 500ms | Primary transport |
| Transport failover | < 2s | Switch to next transport |
| Voice latency | < 150ms | P2P WebRTC |
| TTS generation | < 200ms | Piper on CPU |
| STT transcription | < 1s | Whisper base model |
| File transfer | > 10 MB/s | P2P via Iroh/WireGuard |
| Encryption overhead | < 5ms | Per message |
| Startup time | < 2s | With advocate hydration |

---

*Architecture designed by the smilinTux team + Opus + Lumina.*
*Because chat should be sovereign, not surveilled.* 🐧👑🦀
