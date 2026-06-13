# skcomms ChannelAdapter Interface (Batch C1)

**Date:** 2026-06-13
**Author:** architect pass (Lumina — skcomms design)
**Status:** Proposed — C1 contract; precondition for C2 (Telegram adapter) and C3 (registry)
**Parent spec:** `2026-06-12-skchat-architecture-reassessment.md` §5 Batch C

---

## 1. Problem statement

skcomms has a working sovereign transport layer (17 paths: WebRTC, Tailscale,
Nostr, Syncthing, file, …). Each path carries **signed/encrypted envelopes**
between agents that share a CapAuth identity model. That is the agent-to-agent
plane.

What it does **not** yet have is a formalized way to bridge a third-party
**platform** (Telegram, Slack, Discord, Nextcloud Talk, Teams) into the agent
world. Today the gap is filled by a bespoke Hermes path: when Lumina is in the
DR-Chiro Telegram group, Hermes calls the Telegram Bot API, formats the message,
and writes it directly to skmem-pg. This works but:

- Is not version-controlled in skcomms.
- Cannot be replicated for Slack/Discord/NC-Talk without copy-pasting the
  Hermes glue each time.
- Does not participate in the capauth/FQID identity model (there is no
  FQID↔Telegram-user mapping, no signed envelope, no trust level).
- Writes to memory via a side-channel rather than through the skcomms routing
  layer, so the P0 unified-memory goal (one skmem-pg of record for all
  surfaces) has a structural leak.

**Goal of Batch C1:** define a `ChannelAdapter` abstract base class and
normalized message model that every platform bridge must implement. C2 wires
Telegram; C3 adds the adapter registry so an agent (Lumina) is automatically
reachable on every enabled adapter under a single FQID.

---

## 2. Design principles

1. **The adapter owns the platform edge; skcomms owns the interior.** An adapter
   translates between a foreign platform's wire format and a normalized
   `ChannelMessage`. skcomms then handles identity resolution, routing, memory
   write, and optional envelope signing. The adapter never reads identity state
   directly from CapAuth; it hands a `PlatformIdentity` to the hub and the hub
   resolves or mints a trust-level.

2. **FQID is the anchor.** Every inbound message is annotated with the sender's
   platform identity (`telegram:user_id:123456789`) and the skcomms hub maps
   that to an FQID (`chef@skworld.io`) if a verified binding exists, or to an
   `untrusted` guest FQID otherwise. Outbound messages use the agent's FQID as
   the source display-name/identity in the platform.

3. **No homeserver required.** Each adapter is a small connector (~200–400 LoC)
   that speaks the platform's Bot/Webhook API and the `ChannelAdapter` contract.
   There is no Matrix appservice, no registration YAML, no state-resolution
   loop. The hub is the existing skcomms daemon.

4. **Memory writes go through the hub.** Adapters do not write to skmem-pg
   directly. They call `hub.dispatch_inbound(channel_message)`, which adds the
   message to the agent's memory via the same path used by skchat and
   voice_engine (the P0 unified-memory contract).

5. **Capabilities are declared, not assumed.** Each adapter declares what it
   can deliver: text, files, voice, reactions, threads, read receipts. The hub
   uses this to decide whether to downgrade a rich message (e.g. strip a voice
   note to a transcript) before forwarding outbound.

---

## 3. Normalized message model

```python
# skcomms/adapters/models.py

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class ChannelType(str, Enum):
    """The external platform this message came from / is going to."""
    TELEGRAM   = "telegram"
    SLACK      = "slack"
    DISCORD    = "discord"
    NC_TALK    = "nc_talk"
    TEAMS      = "teams"
    MATRIX     = "matrix"
    # Escape hatch for experimental adapters:
    CUSTOM     = "custom"


class MessageKind(str, Enum):
    """Normalized content type."""
    TEXT      = "text"
    FILE      = "file"
    IMAGE     = "image"
    VOICE     = "voice"    # audio message / voice note
    VIDEO     = "video"
    STICKER   = "sticker"  # platform-specific; degraded to [sticker] on unsupported channels
    REACTION  = "reaction" # emoji reaction on an existing message
    PRESENCE  = "presence" # typing / online status hint


class TrustLevel(str, Enum):
    """How much we trust the sender's claimed identity."""
    UNTRUSTED = "untrusted"   # no verified binding
    VERIFIED  = "verified"    # FQID↔platform-id binding confirmed via CapAuth
    TRUSTED   = "trusted"     # peer vouched by a sovereign peer
    SOVEREIGN = "sovereign"   # CapAuth + Cloud 9 LOCKED


@dataclass
class PlatformIdentity:
    """The sender/recipient as the external platform knows them."""
    channel:        ChannelType
    platform_id:    str           # e.g. "123456789" (Telegram user_id)
    platform_name:  str           # e.g. "Chef David" (display name)
    room_id:        str           # e.g. "-5134021983" (TG chat/group id)
    room_name:      Optional[str] = None

    @property
    def canonical_key(self) -> str:
        """Stable key used for FQID mapping lookups."""
        return f"{self.channel.value}:user:{self.platform_id}"


@dataclass
class ResolvedIdentity:
    """The sender after hub identity resolution."""
    fqid:           str            # e.g. "chef@skworld.io" or "tg_guest_123@telegram.ext"
    trust:          TrustLevel
    platform:       PlatformIdentity
    capauth_fingerprint: Optional[str] = None   # set when trust >= VERIFIED


@dataclass
class MediaAttachment:
    """A file/image/voice payload attached to a message."""
    filename:   str
    mime_type:  str
    size_bytes: int
    url:        Optional[str]  = None   # ephemeral download URL from the platform
    data:       Optional[bytes] = None  # fetched bytes, if pre-fetched


@dataclass
class ChannelMessage:
    """
    The normalized message that crosses the adapter boundary in both directions.

    Inbound:  adapter fills this from the platform event; hub receives it.
    Outbound: hub fills this from a skcomms message or agent response; adapter
              delivers it to the platform.
    """
    # ---- Mandatory fields -------------------------------------------------
    channel:      ChannelType
    kind:         MessageKind
    text:         str                    # plain-text body (may be empty for voice/image)
    sender:       PlatformIdentity
    room_id:      str                    # platform room / chat / channel id

    # ---- Optional content -------------------------------------------------
    attachments:  list[MediaAttachment] = field(default_factory=list)
    reaction_to:  Optional[str]         = None   # platform msg id for REACTION kind
    emoji:        Optional[str]         = None   # reaction emoji

    # ---- Threading / correlation ------------------------------------------
    platform_msg_id:  Optional[str] = None   # original platform message id
    reply_to_platform_id: Optional[str] = None
    skcomms_thread_id:    Optional[str] = None   # skcomms thread (set after hub routing)
    skcomms_envelope_id:  Optional[str] = None   # set after hub wraps into an envelope

    # ---- Metadata ---------------------------------------------------------
    timestamp:    datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    channel_message_id: str = field(
        default_factory=lambda: str(uuid.uuid4())
    )
    raw_payload:  Optional[dict] = None   # original platform event, for debugging
```

---

## 4. The ChannelAdapter contract

```python
# skcomms/adapters/base.py

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from .models import ChannelMessage, ChannelType, PlatformIdentity


@dataclass
class AdapterCapabilities:
    """
    Declare what this adapter can do.

    The hub uses these flags to decide whether to downgrade a rich
    outbound message before forwarding (e.g. strip a voice note to a
    transcript when voice=False).
    """
    text:           bool = True
    files:          bool = True
    images:         bool = True
    voice_notes:    bool = False
    video:          bool = False
    reactions:      bool = False
    threads:        bool = False      # inline threading (Slack threads, TG reply-chain)
    read_receipts:  bool = False
    typing_hint:    bool = False
    max_text_bytes: int  = 4096       # platform message size limit


@dataclass
class AdapterHealth:
    """Point-in-time health snapshot for monitoring."""
    adapter_name:   str
    connected:      bool
    latency_ms:     Optional[float]
    error:          Optional[str] = None
    queued_outbound: int = 0          # messages waiting to be delivered


class ChannelAdapter(ABC):
    """
    Abstract base class for all skcomms channel adapters.

    An adapter is the thin boundary between a foreign platform (Telegram,
    Slack, Discord, …) and the skcomms sovereign hub.  It does three things:

      1. Translate inbound platform events → ChannelMessage.
      2. Translate outbound ChannelMessage → platform API calls.
      3. Map FQID ↔ platform user/room identities.

    It does NOT:
      - Write to skmem-pg directly.
      - Resolve FQID trust levels (that is the hub's job).
      - Hold conversation state beyond what the platform provides.
      - Know about CapAuth keys.

    Lifecycle:
        adapter = TelegramAdapter(config)
        await adapter.connect()              # authenticate + start polling/webhook
        async for msg in adapter.inbound():  # yields normalized ChannelMessages
            await hub.dispatch_inbound(msg)
        await adapter.disconnect()
    """

    # Subclasses must set these:
    channel_type: ChannelType
    adapter_name: str   # e.g. "telegram", "slack-sktechops"

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    @abstractmethod
    async def connect(self) -> None:
        """
        Authenticate with the platform and start the inbound loop.

        Raises AdapterAuthError if credentials are invalid.
        Raises AdapterConnectError if the platform is unreachable.
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully stop the inbound loop and close any open connections."""

    @abstractmethod
    async def health(self) -> AdapterHealth:
        """
        Return a point-in-time health snapshot.

        Called by the adapter registry every 30 s; used by skmon and
        the `skcomms adapter status` CLI.
        """

    # -----------------------------------------------------------------------
    # Inbound (platform → skcomms)
    # -----------------------------------------------------------------------

    @abstractmethod
    async def inbound(self):
        """
        Async generator that yields normalized ChannelMessages as they arrive.

        The hub calls `async for msg in adapter.inbound(): ...`.
        Implementations may use long-polling, webhooks, or WebSocket
        subscriptions — the caller does not care which.

        Yields:
            ChannelMessage: one per platform event.
        """

    # -----------------------------------------------------------------------
    # Outbound (skcomms → platform)
    # -----------------------------------------------------------------------

    @abstractmethod
    async def send(self, message: ChannelMessage) -> str:
        """
        Deliver a ChannelMessage to the platform.

        The hub calls this after resolving the outbound route.
        Must handle rate limiting internally (back off + retry up to
        the adapter's configured timeout, then raise AdapterSendError).

        Args:
            message: Normalized outbound message.  The hub has already
                     applied capability downgrade (e.g. converted voice
                     to a transcript if voice=False).

        Returns:
            The platform's message id for the delivered message.

        Raises:
            AdapterSendError on unrecoverable failure.
        """

    # -----------------------------------------------------------------------
    # Identity mapping (FQID ↔ platform user/room)
    # -----------------------------------------------------------------------

    @abstractmethod
    async def resolve_fqid(self, platform_id: PlatformIdentity) -> Optional[str]:
        """
        Look up the FQID bound to this platform identity.

        Returns the FQID string (e.g. "chef@skworld.io") if a verified
        binding exists, or None if the platform user is unknown.

        The hub calls this on every inbound message and assigns a trust
        level accordingly.  Implementations should consult the adapter's
        local identity map first, then optionally query the CapAuth
        DID registry.
        """

    @abstractmethod
    async def bind_fqid(
        self,
        platform_id: PlatformIdentity,
        fqid: str,
        trust_level: str,
    ) -> None:
        """
        Persist a verified FQID ↔ platform-id binding.

        Called by the hub's identity-binding flow (e.g. when Chef types
        `/bind chef@skworld.io` in the Telegram group and the hub verifies
        the CapAuth challenge).  Implementations write to the adapter's
        own store (YAML / SQLite / skcapstone peers/).
        """

    # -----------------------------------------------------------------------
    # Capabilities declaration (not abstract — safe default provided)
    # -----------------------------------------------------------------------

    def capabilities(self) -> AdapterCapabilities:
        """
        Declare what this platform supports.

        Subclasses should override to return accurate flags.
        The hub uses these for outbound capability downgrade.
        """
        return AdapterCapabilities()
```

---

## 5. Registry and routing

```python
# skcomms/adapters/registry.py

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .base import ChannelAdapter, AdapterHealth
from .models import ChannelMessage, ChannelType, TrustLevel

logger = logging.getLogger("skcomms.adapters")


class AdapterRegistry:
    """
    Maintains the set of live channel adapters and routes messages.

    One registry per skcomms hub instance.  Loaded at daemon startup
    from the adapter config block in ~/.skcomm/config.yml (or the
    skcomms stack's environment).

    Key responsibilities:
      1. Start/stop adapters.
      2. Receive inbound ChannelMessages from each adapter, resolve
         identity, and dispatch to the agent's memory + advocacy engine.
      3. Route outbound messages from the agent to the right adapter(s).
      4. Broadcast presence: the agent appears on ALL enabled adapters
         under a single FQID.
    """

    def __init__(self, hub: "SkcommsHub") -> None:
        self._hub = hub
        self._adapters: dict[str, ChannelAdapter] = {}   # adapter_name → adapter
        self._tasks:    dict[str, asyncio.Task]   = {}

    def register(self, adapter: ChannelAdapter) -> None:
        """Add an adapter.  Must be called before start()."""
        self._adapters[adapter.adapter_name] = adapter

    async def start(self) -> None:
        """Connect all registered adapters and launch their inbound loops."""
        for name, adapter in self._adapters.items():
            await adapter.connect()
            self._tasks[name] = asyncio.create_task(
                self._run_inbound(adapter),
                name=f"adapter-{name}",
            )
            logger.info("adapter %s started", name)

    async def stop(self) -> None:
        for name, task in self._tasks.items():
            task.cancel()
        for name, adapter in self._adapters.items():
            await adapter.disconnect()

    async def _run_inbound(self, adapter: ChannelAdapter) -> None:
        """Drain the adapter's inbound generator and dispatch each message."""
        async for msg in adapter.inbound():
            try:
                await self._dispatch(adapter, msg)
            except Exception:
                logger.exception("dispatch error from %s", adapter.adapter_name)

    async def _dispatch(
        self, adapter: ChannelAdapter, msg: ChannelMessage
    ) -> None:
        """
        Identity-resolve, assign trust, write to unified memory, and
        deliver to the agent's advocacy engine.

        This is the P0 unified-memory boundary: every surface writes
        through here, not via direct skmem-pg calls.
        """
        # 1. Resolve FQID
        fqid = await adapter.resolve_fqid(msg.sender)
        trust = TrustLevel.VERIFIED if fqid else TrustLevel.UNTRUSTED
        if not fqid:
            # Mint a stable guest FQID for this platform identity
            fqid = f"{msg.sender.channel.value}_guest_{msg.sender.platform_id}@ext"

        # 2. Write to unified memory (one skmem-pg of record)
        await self._hub.memory.write_channel_message(msg, fqid=fqid, trust=trust)

        # 3. Hand to the advocacy engine for agent response
        await self._hub.advocacy.on_channel_message(msg, sender_fqid=fqid)

    async def send_to_adapter(
        self,
        adapter_name: str,
        message: ChannelMessage,
    ) -> str:
        """Send an outbound message through a named adapter."""
        adapter = self._adapters[adapter_name]
        caps = adapter.capabilities()
        message = self._downgrade(message, caps)
        return await adapter.send(message)

    async def broadcast_presence(self, agent_fqid: str, status: str) -> None:
        """Push an agent's presence update to all enabled adapters."""
        for adapter in self._adapters.values():
            if adapter.capabilities().typing_hint:
                try:
                    await adapter.set_presence(agent_fqid, status)
                except Exception:
                    logger.debug("presence update skipped on %s", adapter.adapter_name)

    def health_all(self) -> dict[str, AdapterHealth]:
        """Collect health snapshots synchronously (for CLI / monitoring)."""
        import asyncio
        loop = asyncio.get_event_loop()
        return {
            name: loop.run_until_complete(adapter.health())
            for name, adapter in self._adapters.items()
        }

    @staticmethod
    def _downgrade(msg: ChannelMessage, caps: "AdapterCapabilities") -> ChannelMessage:
        """
        Strip content the target adapter cannot render.

        Rules applied in order:
          - voice + caps.voice_notes=False → text = "[Voice note: {transcript}]"
          - image + caps.images=False → text = "[Image: {filename}]", drop attachment
          - text > caps.max_text_bytes → truncate with "… [truncated]"
        """
        from .models import MessageKind
        import dataclasses
        msg = dataclasses.replace(msg)   # shallow copy

        if msg.kind == MessageKind.VOICE and not caps.voice_notes:
            transcript = msg.text or "[untranscribed voice note]"
            msg.kind = MessageKind.TEXT
            msg.text = f"[Voice note: {transcript}]"
            msg.attachments = []

        if msg.kind == MessageKind.IMAGE and not caps.images:
            names = ", ".join(a.filename for a in msg.attachments) or "image"
            msg.text = f"[Image: {names}]"
            msg.attachments = []
            msg.kind = MessageKind.TEXT

        if len(msg.text.encode()) > caps.max_text_bytes:
            trimmed = msg.text.encode()[: caps.max_text_bytes - 20].decode(
                errors="ignore"
            )
            msg.text = trimmed + " … [truncated]"

        return msg
```

---

## 6. Telegram adapter — reference implementation

### What it replaces

Today Lumina participates in the **DR-Chiro Telegram group** (chat id
`-5134021983`) via a bespoke path:

```
Telegram Bot API → Hermes (Python script, not version-controlled)
                         → direct INSERT into skmem-pg
                         → skcapstone advocacy (separate call)
```

Problems with the current path:
- Not in skcomms/skchat repos — lives in Hermes config, invisible to CI.
- Writes to memory outside the skcomms hub → the P0 unified-memory requirement
  is broken (voice-Lumina and Hermes-Lumina have separate write paths).
- Bot sends from the *bot account*, not from a Lumina FQID — no capauth identity.
- No FQID↔Telegram-user binding → Chef cannot get TRUSTED trust level in skcomms.

### What the Telegram adapter provides

```python
# skcomms/adapters/telegram.py

from __future__ import annotations

import asyncio
import logging
from typing import Optional, AsyncIterator

import httpx   # or python-telegram-bot — see note

from .base import ChannelAdapter, AdapterCapabilities, AdapterHealth
from .models import (
    ChannelMessage,
    ChannelType,
    MediaAttachment,
    MessageKind,
    PlatformIdentity,
)

logger = logging.getLogger("skcomms.adapters.telegram")


class TelegramAdapter(ChannelAdapter):
    """
    Telegram Bot API adapter.

    Replaces the bespoke Hermes path for the DR-Chiro group and generalizes
    to any number of configured chats.

    Config block in ~/.skcomm/config.yml:

        adapters:
          telegram:
            enabled: true
            bot_token: "${SKCOMMS_TG_BOT_TOKEN}"
            poll_interval_s: 2
            rooms:
              dr_chiro:
                chat_id: "-5134021983"
                agent_fqid: "lumina@skworld.io"   # which agent owns this chat
                allow_untrusted: true              # accept msgs without FQID binding
            identity_store: "~/.skcomm/adapters/telegram-ids.yaml"
    """

    channel_type = ChannelType.TELEGRAM
    adapter_name = "telegram"

    def __init__(self, config: dict) -> None:
        self._token      = config["bot_token"]
        self._poll_s     = config.get("poll_interval_s", 2)
        self._rooms      = config.get("rooms", {})
        self._id_store   = config.get("identity_store", "~/.skcomm/adapters/telegram-ids.yaml")
        self._last_update_id: int = 0
        self._running    = False
        self._bindings: dict[str, str] = {}   # canonical_key → fqid

    # -- Lifecycle ----------------------------------------------------------

    async def connect(self) -> None:
        """Validate bot token and load identity bindings."""
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"https://api.telegram.org/bot{self._token}/getMe"
            )
            r.raise_for_status()
            me = r.json()["result"]
        logger.info("telegram adapter connected as @%s", me.get("username"))
        self._load_bindings()
        self._running = True

    async def disconnect(self) -> None:
        self._running = False

    async def health(self) -> AdapterHealth:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(
                    f"https://api.telegram.org/bot{self._token}/getMe"
                )
                return AdapterHealth(
                    adapter_name=self.adapter_name,
                    connected=r.status_code == 200,
                    latency_ms=r.elapsed.total_seconds() * 1000,
                )
        except Exception as e:
            return AdapterHealth(
                adapter_name=self.adapter_name,
                connected=False,
                latency_ms=None,
                error=str(e),
            )

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            text=True,
            files=True,
            images=True,
            voice_notes=True,    # Telegram voice messages / audio
            video=True,
            reactions=True,      # Telegram emoji reactions (Bot API 7.0+)
            threads=True,        # reply-chain as thread
            read_receipts=False,
            typing_hint=True,
            max_text_bytes=4096,
        )

    # -- Inbound ------------------------------------------------------------

    async def inbound(self) -> AsyncIterator[ChannelMessage]:
        """Long-poll getUpdates and yield one ChannelMessage per event."""
        while self._running:
            updates = await self._poll()
            for update in updates:
                msg = self._normalize(update)
                if msg:
                    yield msg
            await asyncio.sleep(self._poll_s)

    async def _poll(self) -> list[dict]:
        params = {"offset": self._last_update_id + 1, "timeout": 10}
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(
                    f"https://api.telegram.org/bot{self._token}/getUpdates",
                    params=params,
                )
                r.raise_for_status()
                updates = r.json().get("result", [])
                if updates:
                    self._last_update_id = updates[-1]["update_id"]
                return updates
        except Exception:
            logger.exception("poll error")
            return []

    def _normalize(self, update: dict) -> Optional[ChannelMessage]:
        """
        Translate a raw Telegram update dict into a ChannelMessage.

        Handles text, photo, voice, document, and sticker updates.
        Unknown update types are dropped with a debug log.
        """
        tg_msg = update.get("message") or update.get("edited_message")
        if not tg_msg:
            return None

        chat  = tg_msg["chat"]
        user  = tg_msg.get("from", {})
        sender = PlatformIdentity(
            channel=ChannelType.TELEGRAM,
            platform_id=str(user.get("id", "unknown")),
            platform_name=(
                f"{user.get('first_name','')} {user.get('last_name','')}".strip()
                or user.get("username", "unknown")
            ),
            room_id=str(chat["id"]),
            room_name=chat.get("title") or chat.get("username"),
        )

        # Determine kind and text
        kind = MessageKind.TEXT
        text = tg_msg.get("text") or tg_msg.get("caption") or ""
        attachments: list[MediaAttachment] = []

        if "voice" in tg_msg or "audio" in tg_msg:
            kind = MessageKind.VOICE
            blob = tg_msg.get("voice") or tg_msg.get("audio")
            attachments.append(MediaAttachment(
                filename=blob.get("file_name", "voice.ogg"),
                mime_type=blob.get("mime_type", "audio/ogg"),
                size_bytes=blob.get("file_size", 0),
            ))
        elif "photo" in tg_msg:
            kind = MessageKind.IMAGE
            photo = tg_msg["photo"][-1]   # largest size
            attachments.append(MediaAttachment(
                filename="photo.jpg",
                mime_type="image/jpeg",
                size_bytes=photo.get("file_size", 0),
            ))
        elif "document" in tg_msg:
            kind = MessageKind.FILE
            doc = tg_msg["document"]
            attachments.append(MediaAttachment(
                filename=doc.get("file_name", "file"),
                mime_type=doc.get("mime_type", "application/octet-stream"),
                size_bytes=doc.get("file_size", 0),
            ))
        elif "sticker" in tg_msg:
            kind = MessageKind.STICKER
            s = tg_msg["sticker"]
            text = s.get("emoji", "")

        return ChannelMessage(
            channel=ChannelType.TELEGRAM,
            kind=kind,
            text=text,
            sender=sender,
            room_id=str(chat["id"]),
            platform_msg_id=str(tg_msg["message_id"]),
            reply_to_platform_id=(
                str(tg_msg["reply_to_message"]["message_id"])
                if "reply_to_message" in tg_msg
                else None
            ),
            attachments=attachments,
            raw_payload=update,
        )

    # -- Outbound -----------------------------------------------------------

    async def send(self, message: ChannelMessage) -> str:
        """Deliver a ChannelMessage to Telegram via sendMessage / sendDocument."""
        params: dict = {"chat_id": message.room_id}
        endpoint = "sendMessage"

        if message.kind in (MessageKind.TEXT, MessageKind.STICKER):
            params["text"] = message.text
            if message.reply_to_platform_id:
                params["reply_to_message_id"] = message.reply_to_platform_id
        elif message.kind == MessageKind.FILE and message.attachments:
            endpoint = "sendDocument"
            # files are uploaded separately; simplified here
            params["caption"] = message.text

        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"https://api.telegram.org/bot{self._token}/{endpoint}",
                json=params,
            )
            r.raise_for_status()
            result = r.json()["result"]
            return str(result["message_id"])

    # -- Identity mapping ---------------------------------------------------

    async def resolve_fqid(self, platform_id: PlatformIdentity) -> Optional[str]:
        return self._bindings.get(platform_id.canonical_key)

    async def bind_fqid(
        self,
        platform_id: PlatformIdentity,
        fqid: str,
        trust_level: str,
    ) -> None:
        self._bindings[platform_id.canonical_key] = fqid
        self._save_bindings()

    # -- Private helpers ----------------------------------------------------

    def _load_bindings(self) -> None:
        import yaml
        from pathlib import Path
        p = Path(self._id_store).expanduser()
        if p.exists():
            self._bindings = yaml.safe_load(p.read_text()) or {}

    def _save_bindings(self) -> None:
        import yaml
        from pathlib import Path
        p = Path(self._id_store).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.dump(self._bindings))
```

**Implementation note on the polling library:** The sketch above uses raw
`httpx` for clarity. In production use `python-telegram-bot>=20` (async,
webhook support, auto-retry) — the `TelegramAdapter` simply wraps the PTB
`Application` object and translates `Update` objects to `ChannelMessage`.

---

## 7. FQID ↔ platform identity binding flow

```
Chef types in TG group:  /bind chef@skworld.io
                              │
                    TelegramAdapter.inbound()
                              │ ChannelMessage(text="/bind chef@skworld.io",
                              │               sender=PlatformIdentity(tg:123456789))
                    AdapterRegistry._dispatch()
                              │
                    hub.advocacy.on_channel_message()
                              │  detects /bind command
                    hub.identity.initiate_challenge(
                         fqid="chef@skworld.io",
                         platform=PlatformIdentity(tg:123456789)
                    )
                              │
                    CapAuth challenge-response (DID verify or
                    out-of-band OTP via skchat/skcomms native channel)
                              │ verified ✓
                    adapter.bind_fqid(
                         platform_id=PlatformIdentity(tg:123456789),
                         fqid="chef@skworld.io",
                         trust_level="verified"
                    )
                              │
                    Subsequent TG messages from user 123456789
                    → resolve_fqid() → "chef@skworld.io"
                    → TrustLevel.VERIFIED
                    → written to skmem-pg under chef@skworld.io
```

Guest users (no binding) receive a stable synthetic FQID:
`telegram_guest_<platform_id>@ext` — their messages are stored but trust is
`UNTRUSTED`; the agent may respond but will not give them elevated tool access.

---

## 8. One agent, all adapters: how Lumina appears everywhere

An agent's FQID (`lumina@skworld.io`) is the anchor. The adapter registry maps
that FQID to every room the adapter is configured to serve:

```yaml
# ~/.skcomm/config.yml — adapter section
adapters:
  telegram:
    enabled: true
    bot_token: "${SKCOMMS_TG_BOT_TOKEN}"
    rooms:
      dr_chiro:
        chat_id: "-5134021983"
        agent_fqid: "lumina@skworld.io"
      sk_ops:
        chat_id: "-1001234567890"
        agent_fqid: "lumina@skworld.io"
  slack:
    enabled: false   # C4
    bot_token: "${SKCOMMS_SLACK_BOT_TOKEN}"
    channels:
      general:
        channel_id: "C01234ABCDE"
        agent_fqid: "lumina@skworld.io"
  matrix:
    enabled: false   # C5, opt-in
    homeserver_url: "https://matrix.skworld.io"
    access_token:   "${SKCOMMS_MATRIX_TOKEN}"
    rooms:
      sk_main:
        room_id: "!abc123:matrix.skworld.io"
        agent_fqid: "lumina@skworld.io"
```

When Lumina's advocacy engine produces a reply:

1. The hub looks up which adapter+room the inbound message originated from
   (stored in `ChannelMessage.channel` + `room_id`).
2. `AdapterRegistry.send_to_adapter(adapter_name, outbound_msg)` is called.
3. The adapter applies capability downgrade and delivers to the platform.
4. The memory write for the outbound message also goes through the hub, so
   the same skmem-pg row carries both inbound and outbound, threaded by
   `skcomms_thread_id`.

Lumina does not need to know she is "in Telegram" — she just sees a
`ChannelMessage` with `sender.fqid="chef@skworld.io"` and a request.

---

## 9. Why no homeserver (contrast with Matrix mautrix bridges)

| Dimension | Matrix + mautrix-telegram | skcomms ChannelAdapter |
|-----------|--------------------------|------------------------|
| Infra cost | Homeserver (Postgres, federation, state-resolution) + appservice per bridge | skcomms daemon + one adapter module per platform (~300 LoC) |
| State model | Matrix room state (join/leave events, state-resolution algorithm) | Stateless; room membership is the platform's concern |
| Identity | Matrix `@user:server` + MXID ↔ platform mapping | FQID + platform-id binding stored in a YAML file |
| Federation | Yes — Matrix rooms can federate to any homeserver | None needed; we are sovereign-only with opt-in Funnel/Cloudflare edge |
| Agent first-class | Agents are bot users in the Matrix room | Agents are FQID principals — the routing and memory is built around them |
| When Matrix wins | You want Element/Cinny clients or federation with external orgs | Never for the base case; exactly when C5 is warranted |
| Escape hatch | — | Matrix is one optional adapter (C5); mautrix libraries remain usable as the implementation layer |

**The key asymmetry:** Matrix-as-foundation means every other transport is a
bridge *into* Matrix (the hub becomes Matrix). skcomms-as-hub means Matrix is
one more adapter — used only when Element clients or federation are worth the
weight. For a Tailscale-native sovereign fleet of 5–10 agents that already has
skcomms + a working Telegram path, lightweight adapters win decisively.

---

## 10. Unified memory (P0 contract)

The P0 requirement from the reassessment doc is: **one Lumina identity → one
memory + context whether reached by voice, text chat, a bridged platform, or
Hermes.**

How the adapter layer satisfies it:

```
┌─── inbound path ───────────────────────────────────────────────┐
│  TelegramAdapter.inbound()                                      │
│       │ ChannelMessage                                          │
│  AdapterRegistry._dispatch()                                    │
│       │ resolved FQID + trust                                   │
│  SkcommsHub.memory.write_channel_message()  ←── ONE write path │
│       │                                          to skmem-pg   │
│  SkcommsHub.advocacy.on_channel_message()                       │
│       │ agent response (ChannelMessage)                         │
│  AdapterRegistry.send_to_adapter()                              │
│       │ platform delivery                                        │
│  SkcommsHub.memory.write_channel_message()  ←── outbound too   │
└─────────────────────────────────────────────────────────────────┘
```

`write_channel_message` is the single skmem-pg write function shared by:
- The adapter registry (Batch C).
- `skchat.memory_bridge` (Batch A's MemoryBridge).
- `voice_engine.MemoryBridge` (Batch A).

After C1+C2, the Hermes direct-write path is **removed**. The DR-Chiro
Telegram group's messages flow through `TelegramAdapter → AdapterRegistry →
write_channel_message` just like a skchat message.

---

## 11. File layout

```
skcomms/
└── src/skcomms/
    └── adapters/
        ├── __init__.py          # exports ChannelAdapter, ChannelMessage, AdapterRegistry
        ├── models.py            # ChannelMessage, PlatformIdentity, etc. (§3)
        ├── base.py              # ChannelAdapter ABC (§4)
        ├── registry.py          # AdapterRegistry (§5)
        ├── telegram.py          # TelegramAdapter (§6) — C2
        ├── slack.py             # SlackAdapter — C4
        ├── discord.py           # DiscordAdapter — C4
        ├── nc_talk.py           # NextcloudTalkAdapter — C4/D8
        └── matrix.py            # MatrixAdapter (mautrix) — C5 optional
```

Config extension to `~/.skcomm/config.yml`:

```yaml
adapters:
  telegram:
    enabled: true
    bot_token: "${SKCOMMS_TG_BOT_TOKEN}"
    poll_interval_s: 2
    rooms:
      dr_chiro:
        chat_id: "-5134021983"
        agent_fqid: "lumina@skworld.io"
    identity_store: "~/.skcomm/adapters/telegram-ids.yaml"
```

The `AdapterRegistry` is instantiated inside the existing `skcomms` daemon
(the async event loop already running for transport polling). No new process
is needed.

---

## 12. Acceptance criteria

### C1 — Interface defined (this spec)
- [ ] `ChannelMessage`, `PlatformIdentity`, `ChannelAdapter`, `AdapterRegistry`
      implemented in `src/skcomms/adapters/`.
- [ ] `AdapterCapabilities` and `_downgrade()` cover text/image/voice/file
      downgrade paths.
- [ ] `AdapterRegistry.start()` / `stop()` / `health_all()` wired.
- [ ] Unit tests: `test_registry_dispatch.py` — mock adapter yields 3 messages,
      registry dispatches and writes to mock hub; verify FQID resolution and
      trust assignment.
- [ ] `skcomms adapter status` CLI subcommand shows health of all registered
      adapters.

### C2 — Telegram adapter (reference impl)
- [ ] `TelegramAdapter` passes: text/voice/image/file normalization tests.
- [ ] DR-Chiro group messages route through `TelegramAdapter` (not Hermes).
- [ ] Hermes direct-skmem-pg write path retired for this group.
- [ ] Chef's Telegram user id is bound to `chef@skworld.io` (verified).
- [ ] Memory writes appear in skmem-pg under `agent=lumina`, sourced as
      `channel=telegram`, thread-linked by `skcomms_thread_id`.
- [ ] Lumina's outbound replies appear in the DR-Chiro group within 3 s.

### C3 — Adapter registry + agent presence
- [ ] YAML adapter config accepted; adapters loaded at daemon startup.
- [ ] Agent appears on all enabled adapters under one FQID; identity bindings
      persist across daemon restarts.
- [ ] `broadcast_presence` sends typing indicator on adapters that support it.

---

## 13. Open questions

1. **Webhook vs long-poll for Telegram.** Long-poll is simpler for the initial
   impl and works without a public endpoint (keeps Tailscale-first). Webhooks
   give lower latency but require a Funnel/Cloudflare URL. Recommendation:
   start with long-poll (C2); add webhook support as a config option in C3.

2. **Voice-note transcription in the adapter layer.** Should `TelegramAdapter`
   fetch and transcribe voice notes before yielding the `ChannelMessage` (so
   `msg.text` is always populated), or should the hub do it? Leaning toward:
   adapter yields the raw attachment with `kind=VOICE`; the hub passes it to
   `voice_engine.transcribe()` if the agent needs a text representation. This
   keeps the adapter stateless.

3. **FQID↔platform binding UX.** The `/bind chef@skworld.io` slash-command
   flow is proposed but not yet designed in detail. The CapAuth challenge could
   be: hub sends a one-time code to Chef's skchat DM, Chef pastes it in the
   Telegram group → verified. Needs a short design note before C3.

4. **Multi-agent rooms.** When Lumina and Opus are both `agent_fqid` for the
   same room (e.g. a DR-Chiro group that wants both), does the registry
   broadcast to both, or does the advocacy engine handle the routing? Current
   assumption: one `agent_fqid` per room config entry; multi-agent rooms are a
   C3 extension.

5. **Rate-limit and cost exposure.** The Telegram Bot API is free but
   rate-limited (30 msg/s per bot). Slack/Discord have per-workspace costs and
   API limits. The `AdapterCapabilities` model should gain a `rate_limit_per_s`
   field for the registry to apply backpressure. Track in C3.

6. **Envelope signing for channel messages.** Inbound messages from third-party
   platforms cannot carry CapAuth PGP signatures. Should the hub sign the
   *normalized* `ChannelMessage` before writing to skmem-pg (proving it was
   received and processed by skcomms at time T)? This would give an audit trail
   but adds latency. Flag for C3 discussion.
