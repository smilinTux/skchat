"""SKChat transport bridge — wires ChatMessage to SKComm for P2P delivery.

This is the glue between SKChat and SKComm: it takes a ChatMessage,
optionally encrypts it via ChatCrypto, wraps it in an SKComm
MessageEnvelope, and sends it through whatever transports SKComm
has available (Syncthing, file, Nostr, etc).

On the receive side, it polls SKComm for inbound envelopes,
extracts the ChatMessage payload, and stores it in ChatHistory.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from .history import ChatHistory
from .models import ChatMessage, ContentType, DeliveryStatus

logger = logging.getLogger("skchat.transport")


class ChatTransport:
    """Bridge between SKChat and SKComm for P2P message delivery.

    Handles the full lifecycle: compose -> encrypt -> envelope -> route,
    and receive -> deserialize -> decrypt -> store.

    Args:
        skcomm: An SKComm instance for transport.
        history: A ChatHistory instance for persistence.
        crypto: Optional ChatCrypto for encryption/signing.
        identity: CapAuth identity URI for the local user.
        presence_cache: Optional PresenceCache for typing indicator tracking.
    """

    SKCHAT_CONTENT_KEY = "skchat_message"

    def __init__(
        self,
        skcomm: object,
        history: ChatHistory,
        crypto: Optional[object] = None,
        identity: str = "capauth:local@skchat",
        presence_cache: Optional[object] = None,
    ) -> None:
        self._skcomm = skcomm
        self._history = history
        self._crypto = crypto
        self._identity = identity
        self._presence_cache = presence_cache  # PresenceCache for typing indicators

    @property
    def identity(self) -> str:
        """The local user's identity URI.

        Returns:
            str: CapAuth identity URI.
        """
        return self._identity

    def send_message(
        self,
        message: ChatMessage,
        recipient_public_armor: Optional[str] = None,
    ) -> dict:
        """Send a ChatMessage via SKComm.

        Encrypts (if crypto and public key are available), serializes
        the ChatMessage into the SKComm envelope payload, and routes
        it through available transports.

        Args:
            message: The ChatMessage to send.
            recipient_public_armor: Optional PGP public key for encryption.

        Returns:
            dict: Delivery report with 'delivered' bool and details.
        """
        outbound = message.model_copy()

        if self._crypto and recipient_public_armor:
            try:
                outbound = self._crypto.encrypt_message(outbound, recipient_public_armor)
                outbound = self._crypto.sign_message(outbound)
            except Exception as exc:
                logger.warning("Encryption failed, sending plaintext: %s", exc)

        payload_json = outbound.model_dump_json()

        try:
            report = self._skcomm.send(
                recipient=message.recipient,
                message=payload_json,
                thread_id=message.thread_id,
                in_reply_to=message.reply_to_id,
            )

            delivered = getattr(report, "delivered", False)

            stored_msg = message.model_copy(
                update={
                    "delivery_status": (
                        DeliveryStatus.SENT if delivered else DeliveryStatus.FAILED
                    ),
                }
            )
            self._history.store_message(stored_msg)

            return {
                "delivered": delivered,
                "message_id": message.id,
                "recipient": message.recipient,
                "transport": getattr(report, "successful_transport", None),
            }

        except Exception as exc:
            logger.error("SKComm send failed: %s", exc)

            failed_msg = message.model_copy(
                update={"delivery_status": DeliveryStatus.FAILED}
            )
            self._history.store_message(failed_msg)

            return {
                "delivered": False,
                "message_id": message.id,
                "recipient": message.recipient,
                "error": str(exc),
            }

    def poll_inbox(
        self,
        sender_public_armor: Optional[str] = None,
    ) -> list[ChatMessage]:
        """Poll SKComm for incoming messages and store them.

        Receives all pending envelopes from SKComm, extracts
        ChatMessage payloads, optionally decrypts them, stores
        in ChatHistory, and returns the messages.

        Args:
            sender_public_armor: Optional PGP public key for
                signature verification on incoming messages.

        Returns:
            list[ChatMessage]: Newly received ChatMessages.
        """
        try:
            envelopes = self._skcomm.receive()
        except Exception as exc:
            logger.error("SKComm receive failed: %s", exc)
            return []

        messages: list[ChatMessage] = []

        for envelope in envelopes:
            try:
                # Route HEARTBEAT envelopes to presence/typing handler
                try:
                    from skcomm.models import MessageType as _MsgType

                    if getattr(envelope, "message_type", None) == _MsgType.HEARTBEAT:
                        self._handle_heartbeat(envelope)
                        continue
                except ImportError:
                    pass

                payload_content = self._extract_payload(envelope)
                if payload_content is None:
                    continue

                msg = ChatMessage.model_validate_json(payload_content)

                if self._crypto and msg.encrypted:
                    try:
                        msg = self._crypto.decrypt_message(msg)
                    except Exception as exc:
                        logger.warning("Decryption failed for %s: %s", msg.id[:8], exc)

                if sender_public_armor and msg.signature and self._crypto:
                    from .crypto import ChatCrypto

                    if not ChatCrypto.verify_signature(msg, sender_public_armor):
                        logger.warning("Invalid signature on message %s", msg.id[:8])
                        msg.metadata["signature_valid"] = False
                    else:
                        msg.metadata["signature_valid"] = True

                msg = msg.model_copy(
                    update={"delivery_status": DeliveryStatus.DELIVERED}
                )
                self._history.store_message(msg)
                messages.append(msg)

            except Exception as exc:
                logger.debug("Failed to process envelope: %s", exc)

        if messages:
            logger.info("Received %d chat message(s)", len(messages))

        return messages

    def send_and_store(
        self,
        recipient: str,
        content: str,
        thread_id: Optional[str] = None,
        reply_to: Optional[str] = None,
        ttl: Optional[int] = None,
        recipient_public_armor: Optional[str] = None,
    ) -> dict:
        """Convenience method: compose, send, and store in one call.

        Args:
            recipient: CapAuth identity URI of the recipient.
            content: Message content text.
            thread_id: Optional thread identifier.
            reply_to: Optional message ID being replied to.
            ttl: Optional seconds until auto-delete.
            recipient_public_armor: Optional PGP public key for encryption.

        Returns:
            dict: Delivery report.
        """
        message = ChatMessage(
            sender=self._identity,
            recipient=recipient,
            content=content,
            content_type=ContentType.MARKDOWN,
            thread_id=thread_id,
            reply_to_id=reply_to,
            ttl=ttl,
        )

        return self.send_message(message, recipient_public_armor)

    @staticmethod
    def _extract_payload(envelope: object) -> Optional[str]:
        """Extract the message content from an SKComm envelope.

        Handles both MessageEnvelope objects and raw dicts.

        Args:
            envelope: An SKComm MessageEnvelope or dict.

        Returns:
            Optional[str]: The payload content string, or None.
        """
        if hasattr(envelope, "payload"):
            payload = envelope.payload
            if hasattr(payload, "content"):
                return payload.content
        elif isinstance(envelope, dict):
            payload = envelope.get("payload", {})
            if isinstance(payload, dict):
                return payload.get("content")
            return str(payload)
        return None

    def send_typing_indicator(
        self,
        recipient: str,
        thread_id: Optional[str] = None,
    ) -> None:
        """Send a typing presence indicator to a recipient via HEARTBEAT.

        The recipient's UI can use this to display a typing animation while
        this agent is composing a reply.  Failures are logged at DEBUG only.

        Args:
            recipient: CapAuth identity URI of the recipient.
            thread_id: Optional thread the typing is happening in.
        """
        from .presence import PresenceIndicator, PresenceState

        indicator = PresenceIndicator(
            identity_uri=self._identity,
            state=PresenceState.TYPING,
            thread_id=thread_id,
        )
        try:
            from skcomm.models import MessageType

            self._skcomm.send(
                recipient=recipient,
                message=indicator.model_dump_json(),
                message_type=MessageType.HEARTBEAT,
            )
        except Exception as exc:
            logger.debug("Typing indicator send failed: %s", exc)

    def _handle_heartbeat(self, envelope: object) -> None:
        """Process an incoming HEARTBEAT envelope for presence/typing state.

        If a presence_cache is wired in and the payload is a PresenceIndicator
        with TYPING state, records the typing signal.  Non-TYPING heartbeats
        clear any existing typing indicator for the sender.

        Args:
            envelope: An SKComm MessageEnvelope with message_type=HEARTBEAT.
        """
        if self._presence_cache is None:
            return
        payload_content = self._extract_payload(envelope)
        if not payload_content:
            return
        try:
            from .presence import PresenceIndicator, PresenceState

            indicator = PresenceIndicator.model_validate_json(payload_content)
            is_typing = indicator.state == PresenceState.TYPING
            self._presence_cache.set_typing(indicator.identity_uri, is_typing)
        except Exception as exc:
            logger.debug("HEARTBEAT presence parse failed: %s", exc)
