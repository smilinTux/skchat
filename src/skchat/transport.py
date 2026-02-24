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
    """

    SKCHAT_CONTENT_KEY = "skchat_message"

    def __init__(
        self,
        skcomm: object,
        history: ChatHistory,
        crypto: Optional[object] = None,
        identity: str = "capauth:local@skchat",
    ) -> None:
        self._skcomm = skcomm
        self._history = history
        self._crypto = crypto
        self._identity = identity

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
                in_reply_to=message.reply_to,
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
            reply_to=reply_to,
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
