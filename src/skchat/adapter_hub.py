"""AdapterHub — bridge inbound skcomms ChannelMessages into skchat.

The :class:`AdapterHub` is the skchat-side landing zone for normalized
channel messages produced by the skcomms channel-adapter layer
(:mod:`skcomms.adapters`).  An external surface (Telegram, Slack, NC Talk, …)
hands its platform event to a skcomms ``ChannelAdapter``, which normalizes it
into a :class:`~skcomms.adapters.models.ChannelMessage`.  That message is then
delivered here, where the hub:

1. Converts the ``ChannelMessage`` into a skchat
   :class:`~skchat.models.ChatMessage`, preserving text, sender, and
   timestamp.
2. Resolves the platform sender to a sovereign FQID via an **injectable**
   ``resolve_fqid`` callable.  When ``resolve_fqid`` is ``None`` or the
   resolution yields no FQID, the sender is marked **UNTRUSTED** and a stable
   synthetic guest FQID is minted from the platform identity.
3. Writes the converted ChatMessage to :class:`~skchat.history.ChatHistory`
   (the unified-memory write).
4. Fires the advocacy dispatch path
   (:meth:`~skchat.advocacy.AdvocacyEngine.process_message`) so the agent can
   reply when the message contains a trigger.

Every dependency is injectable so the hub is fully unit-testable with mocks —
no network, no real adapters, no skmem-pg.

Example::

    hub = AdapterHub(
        history=ChatHistory(history_dir=tmp),
        advocacy=AdvocacyEngine(),
        resolve_fqid=lambda ident: known_map.get(ident.canonical_key),
        agent_identity="capauth:opus@skworld.io",
    )
    result = hub.handle_inbound(channel_message)
    if result.reply:
        adapter.send(result.reply)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .history import ChatHistory
from .models import ChatMessage, ContentType

logger = logging.getLogger("skchat.adapter_hub")

# Trust markers.  We mirror the skcomms TrustLevel vocabulary but keep our own
# string constants so the hub does not hard-depend on the skcomms enum being
# importable at call time (the values match skcomms.adapters.models.TrustLevel).
TRUST_UNTRUSTED = "untrusted"
TRUST_VERIFIED = "verified"

# Callable that maps a skcomms PlatformIdentity to a sovereign FQID string.
# Returns None / "" when the platform sender has no verified binding.
ResolveFqid = Callable[[Any], Optional[str]]


@dataclass
class InboundResult:
    """Outcome of handling one inbound channel message.

    Attributes:
        message: The converted skchat :class:`ChatMessage` that was stored.
        fqid: The resolved (or synthetic) sovereign FQID of the sender.
        trust: ``"verified"`` when ``resolve_fqid`` produced an FQID, else
            ``"untrusted"``.
        reply: The advocacy auto-response string, or ``None`` when the message
            did not trigger advocacy (or advocacy is disabled).
    """

    message: ChatMessage
    fqid: str
    trust: str
    reply: Optional[str] = None

    @property
    def is_trusted(self) -> bool:
        """True when the sender resolved to a verified FQID."""
        return self.trust == TRUST_VERIFIED


class AdapterHub:
    """Receives inbound skcomms ChannelMessages and routes them into skchat.

    The hub is intentionally framework-light: every collaborator is injected,
    so the class can be exercised end-to-end with plain mocks.

    Args:
        hub: Optional parent hub object.  When provided and ``history`` /
            ``advocacy`` / ``resolve_fqid`` are not passed explicitly, the hub
            attributes ``hub.history``, ``hub.advocacy`` and
            ``hub.resolve_fqid`` are used as fallbacks.  Pure convenience for
            production wiring; not required for testing.
        history: A :class:`ChatHistory` (or any object exposing ``save``).
        advocacy: An advocacy engine exposing ``process_message(ChatMessage)``
            that returns ``Optional[str]``.  ``None`` disables advocacy.
        resolve_fqid: Callable mapping a skcomms ``PlatformIdentity`` to a
            sovereign FQID string, or ``None``.  When ``None`` (or the call
            returns a falsy value) the sender is marked UNTRUSTED.
        agent_identity: CapAuth identity URI used as the ``recipient`` of the
            converted ChatMessage (the agent the message is addressed to).
    """

    DEFAULT_AGENT_IDENTITY: str = "capauth:opus@skworld.io"

    def __init__(
        self,
        hub: object = None,
        *,
        history: Optional[ChatHistory] = None,
        advocacy: object = None,
        resolve_fqid: Optional[ResolveFqid] = None,
        agent_identity: Optional[str] = None,
    ) -> None:
        self._hub = hub

        # Resolve collaborators, falling back to hub.* when not given.
        if history is None and hub is not None:
            history = getattr(hub, "history", None)
        if advocacy is None and hub is not None:
            advocacy = getattr(hub, "advocacy", None)
        if resolve_fqid is None and hub is not None:
            resolve_fqid = getattr(hub, "resolve_fqid", None)
        if agent_identity is None and hub is not None:
            agent_identity = getattr(hub, "agent_identity", None)

        self._history = history
        self._advocacy = advocacy
        self._resolve_fqid = resolve_fqid
        self._agent_identity = agent_identity or self.DEFAULT_AGENT_IDENTITY

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def agent_identity(self) -> str:
        """The CapAuth identity URI the hub delivers messages to."""
        return self._agent_identity

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve_sender(self, channel_message: Any) -> tuple[str, str]:
        """Resolve a ChannelMessage sender to ``(fqid, trust)``.

        Applies the injected ``resolve_fqid`` callable to the message's
        :class:`PlatformIdentity`.  When the callable is ``None``, raises, or
        returns a falsy value, the sender is marked UNTRUSTED and a stable
        synthetic guest FQID is minted from the platform identity:
        ``"{channel}_guest_{platform_id}@ext"``.

        Args:
            channel_message: The inbound skcomms ChannelMessage.

        Returns:
            tuple[str, str]: ``(fqid, trust)`` where *trust* is one of
            ``"verified"`` / ``"untrusted"``.
        """
        platform = channel_message.sender
        fqid: Optional[str] = None

        if self._resolve_fqid is not None:
            try:
                fqid = self._resolve_fqid(platform)
            except Exception as exc:  # defensive: a bad resolver must not crash
                logger.warning("adapter_hub: resolve_fqid raised: %s", exc)
                fqid = None

        if fqid:
            return fqid, TRUST_VERIFIED

        # Unresolved → UNTRUSTED, mint a stable guest FQID.
        return self._guest_fqid(platform), TRUST_UNTRUSTED

    def to_chat_message(
        self,
        channel_message: Any,
        sender_fqid: str,
        trust: str,
    ) -> ChatMessage:
        """Convert a skcomms ChannelMessage into a skchat ChatMessage.

        Preserves the platform text, the resolved sender FQID, and the original
        timestamp.  Platform/channel provenance is captured in ``metadata`` so
        nothing is lost.  Messages with no text but carrying attachments get a
        placeholder body so the :class:`ChatMessage` content-or-attachments
        invariant is satisfied (skchat attachments are a separate transfer
        concept, so we degrade media to a textual marker).

        Args:
            channel_message: The inbound skcomms ChannelMessage.
            sender_fqid: The resolved sovereign FQID of the sender.
            trust: The trust marker (``"verified"`` / ``"untrusted"``).

        Returns:
            ChatMessage: The converted, ready-to-store message.
        """
        platform = channel_message.sender

        text = channel_message.text or ""
        if not text.strip():
            # No usable body — degrade media/empty kinds to a textual marker so
            # the ChatMessage content invariant holds.
            kind = self._kind_value(channel_message)
            text = f"[{kind}]" if kind else "[message]"

        timestamp = self._coerce_timestamp(getattr(channel_message, "timestamp", None))

        metadata: dict[str, Any] = {
            "source": "channel_adapter",
            "channel": self._enum_value(getattr(channel_message, "channel", None)),
            "kind": self._kind_value(channel_message),
            "trust": trust,
            "platform_id": getattr(platform, "platform_id", None),
            "platform_name": getattr(platform, "platform_name", None),
            "room_id": getattr(channel_message, "room_id", None)
            or getattr(platform, "room_id", None),
            "platform_msg_id": getattr(channel_message, "platform_msg_id", None),
            "channel_message_id": getattr(channel_message, "channel_message_id", None),
        }
        if trust == TRUST_UNTRUSTED:
            metadata["untrusted"] = True

        return ChatMessage(
            sender=sender_fqid,
            recipient=self._agent_identity,
            content=text,
            content_type=ContentType.PLAIN,
            timestamp=timestamp,
            metadata=metadata,
        )

    def handle_inbound(self, channel_message: Any) -> InboundResult:
        """Process one inbound ChannelMessage end-to-end.

        Pipeline:
          1. Resolve the sender FQID + trust (UNTRUSTED when unresolved).
          2. Convert to a skchat :class:`ChatMessage`.
          3. Persist it to :class:`ChatHistory`.
          4. Fire the advocacy dispatch path; capture any reply.

        Args:
            channel_message: The inbound skcomms ChannelMessage.

        Returns:
            InboundResult: The stored message, resolved FQID, trust marker, and
            optional advocacy reply.
        """
        fqid, trust = self.resolve_sender(channel_message)
        chat_msg = self.to_chat_message(channel_message, fqid, trust)

        self._write_history(chat_msg)
        reply = self._dispatch_advocacy(chat_msg)

        return InboundResult(message=chat_msg, fqid=fqid, trust=trust, reply=reply)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_history(self, chat_msg: ChatMessage) -> None:
        """Persist *chat_msg* to history, tolerating a missing store."""
        if self._history is None:
            logger.debug("adapter_hub: no history configured; skipping write")
            return
        self._history.save(chat_msg)

    def _dispatch_advocacy(self, chat_msg: ChatMessage) -> Optional[str]:
        """Fire the advocacy engine and return its reply (or None)."""
        if self._advocacy is None:
            return None
        try:
            return self._advocacy.process_message(chat_msg)
        except Exception as exc:  # advocacy failure must not drop the message
            logger.error("adapter_hub: advocacy dispatch failed: %s", exc)
            return None

    @staticmethod
    def _guest_fqid(platform: Any) -> str:
        """Mint a stable synthetic guest FQID for an unresolved sender."""
        channel = AdapterHub._enum_value(getattr(platform, "channel", None)) or "unknown"
        platform_id = getattr(platform, "platform_id", None) or "anon"
        return f"{channel}_guest_{platform_id}@ext"

    @staticmethod
    def _enum_value(value: Any) -> Optional[str]:
        """Return ``value.value`` for enums, the str() otherwise, None if None."""
        if value is None:
            return None
        return getattr(value, "value", value if isinstance(value, str) else str(value))

    @staticmethod
    def _kind_value(channel_message: Any) -> Optional[str]:
        """Extract the normalized message kind as a plain string."""
        return AdapterHub._enum_value(getattr(channel_message, "kind", None))

    @staticmethod
    def _coerce_timestamp(value: Any) -> datetime:
        """Coerce a ChannelMessage timestamp into an aware UTC datetime.

        Accepts ``datetime`` (naive treated as UTC), ISO-8601 strings, and
        falls back to ``now(UTC)`` for anything unparseable or missing.
        """
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                return datetime.now(timezone.utc)
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc)
