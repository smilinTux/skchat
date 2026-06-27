"""SKChat transport bridge — wires ChatMessage to SKComms for P2P delivery.

This is the glue between SKChat and SKComms: it takes a ChatMessage,
optionally encrypts it via ChatCrypto, wraps it in an SKComms
MessageEnvelope, and sends it through whatever transports SKComms
has available (Syncthing, file, Nostr, etc).

On the receive side, it polls SKComms for inbound envelopes,
extracts the ChatMessage payload, and stores it in ChatHistory.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from .history import ChatHistory
from .models import ChatMessage, ContentType, DeliveryStatus

logger = logging.getLogger("skchat.transport")

# Local file outbox that lumina-bridge's poll_outbox_for_lumina() scans.
_LOCAL_OUTBOX = Path("~/.skcomms/outbox").expanduser()

# Per-fingerprint file inbox root — the standard path for local P2P delivery.
# Each agent polls ~/.skcomms/transport/file/inbox/<fingerprint>/ for incoming
# messages from peers on the same machine.
_FILE_INBOX_ROOT = Path("~/.skcomms/transport/file/inbox").expanduser()

# Local peers on the same machine: messages to these identities get a loopback
# copy written directly to the file-transport outbox so lumina-bridge's
# poll_outbox_for_lumina() can pick them up immediately, independent of the
# SQLite history backlog.
_LOCAL_PEERS: frozenset[str] = frozenset(
    {
        "capauth:lumina@skworld.io",
        "capauth:lumina@capauth.local",
        "lumina@skworld.io",
        "lumina",
    }
)


def _urllib_get_json(url: str, *, timeout: float = 4.0) -> Optional[object]:
    """Real S2S getter: GET *url* and return parsed JSON (or None).

    The production transport for :func:`prekey_exchange.fetch_peer_prekey` — a
    plain stdlib ``urllib`` GET (no new dependency). Returns the decoded JSON
    object, or ``None`` for an empty body / >=400 status. Network/parse errors
    propagate to ``fetch_peer_prekey``, which already wraps the getter in
    try/except and degrades to the classical path. Tests inject a stub instead,
    so this is never exercised against the network in CI.
    """
    import urllib.request

    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        if getattr(resp, "status", 200) >= 400:
            return None
        data = resp.read()
    if not data:
        return None
    return json.loads(data.decode("utf-8"))


def _write_local_loopback(message: ChatMessage) -> None:
    """Write a plaintext envelope to ~/.skcomms/outbox/ for same-machine delivery.

    When SKComms routes via Syncthing (priority 1), the envelope lands in
    ~/.skcapstone/sync/comms/outbox/<peer>/ — a path that lumina-bridge does
    NOT scan.  This function writes a second, unencrypted copy directly to
    ~/.skcomms/outbox/ so poll_outbox_for_lumina() picks it up on the next
    3-second poll cycle.

    Key properties:
      - Always uses plaintext message.model_dump_json() (never encrypted copy)
      - Atomic tmp→rename so the bridge never reads a partial file
      - data["recipient"] matches LUMINA_IDENTITY_VARIANTS
      - data["payload"]["content"] contains the ChatMessage JSON
      - data["payload"]["content_type"] == "text"
    """
    outbox = _LOCAL_OUTBOX
    outbox.mkdir(parents=True, exist_ok=True)

    envelope_id = str(uuid.uuid4())
    envelope = {
        "skcomms_version": "1.0.0",
        "envelope_id": envelope_id,
        "sender": message.sender,
        "recipient": message.recipient,
        "payload": {
            "content": message.model_dump_json(),
            "content_type": "text",
            "encrypted": False,
            "compressed": False,
            "signature": None,
        },
        "routing": {
            "mode": "failover",
            "preferred_transports": [],
            "retry_max": 2,
            "retry_backoff": [5, 15, 60, 300, 900],
            "ttl": 86400,
            "ack_requested": False,
        },
        "metadata": {
            "thread_id": message.thread_id,
            "in_reply_to": message.reply_to_id,
            "urgency": "normal",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "expires_at": None,
            "attempt": 0,
            "delivered_via": "local_loopback",
        },
    }

    filename = f"{envelope_id}.skc.json"
    target = outbox / filename
    tmp = outbox / f".{filename}.tmp"
    try:
        tmp.write_bytes(json.dumps(envelope).encode("utf-8"))
        tmp.rename(target)
        logger.info(
            "Loopback delivery: wrote %s to outbox for %s → %s",
            envelope_id[:8],
            message.sender,
            message.recipient,
        )
    except OSError as exc:
        logger.warning("Failed to write local loopback copy: %s", exc)


class ChatTransport:
    """Bridge between SKChat and SKComms for P2P message delivery.

    Handles the full lifecycle: compose -> encrypt -> envelope -> route,
    and receive -> deserialize -> decrypt -> store.

    Args:
        skcomms: An SKComms instance for transport.
        history: A ChatHistory instance for persistence.
        crypto: Optional ChatCrypto for encryption/signing.
        identity: CapAuth identity URI for the local user.
        presence_cache: Optional PresenceCache for typing indicator tracking.
    """

    SKCHAT_CONTENT_KEY = "skchat_message"

    def __init__(
        self,
        skcomms: object,
        history: ChatHistory,
        crypto: Optional[object] = None,
        identity: str = "capauth:local@skchat",
        presence_cache: Optional[object] = None,
        fallback_transport: Optional[object] = None,
    ) -> None:
        self._skcomms = skcomms
        self._history = history
        self._crypto = crypto
        self._identity = identity
        self._presence_cache = presence_cache  # PresenceCache for typing indicators
        self._fallback_transport = fallback_transport
        # Per-fingerprint inbox root; override in tests to use a tmp dir
        self._file_inbox_root: Path = _FILE_INBOX_ROOT
        # First-contact prekey fetch (RFC-0001 P1 cross-node). Injectable so the
        # wiring is unit-tested without the network: the HTTP getter and the
        # federation inbox resolver default to the real implementations, and
        # ``_prekey_fetch_attempted`` makes the pull one-shot per process so a
        # classical / unroutable peer is never re-hammered.
        self._prekey_http_get = _urllib_get_json
        self._prekey_inbox_resolver: Optional[object] = None  # None => skcomms default
        self._prekey_fetch_attempted: set[str] = set()

    @classmethod
    def from_config(
        cls,
        skcomms: object,
        history: object,
        identity: str = "capauth:local@skchat",
        **kwargs: object,
    ) -> "ChatTransport":
        """Create a ChatTransport from runtime objects.

        Args:
            skcomms: SKComms transport instance.
            history: ChatHistory instance.
            identity: CapAuth identity URI.
            **kwargs: Additional keyword arguments forwarded to __init__.

        Returns:
            ChatTransport: Configured instance.
        """
        return cls(skcomms=skcomms, history=history, identity=identity, **kwargs)

    @property
    def identity(self) -> str:
        """The local user's identity URI.

        Returns:
            str: CapAuth identity URI.
        """
        return self._identity

    def _federation_target(self, recipient: str) -> Optional[str]:
        """Resolve a recipient to a federation peer FQID, or None.

        Returns the peer's ``<agent>@<operator>.<realm>`` FQID iff the recipient
        matches a skcomms peer that advertises an ``https-s2s`` inbox_url (i.e. a
        reachable remote node). Accepts the recipient as a bare name (``lumina``),
        a capauth URI (``capauth:lumina@skworld.io``), or an FQID
        (``lumina@chef.skworld``). Returns None for local/unknown recipients so
        the caller falls back to the legacy local transports.
        """
        try:
            from skcomms.discovery import PeerStore

            norm = recipient.split(":", 1)[1] if recipient.startswith("capauth:") else recipient
            short = norm.split("@", 1)[0]
            wanted = {recipient, norm, short}
            for p in PeerStore().list_all():
                names = {p.name, p.fqid, (p.fqid or "").split("@", 1)[0]}
                if (wanted & names) and p.inbox_url():
                    return p.fqid or norm
        except Exception as exc:  # noqa: BLE001
            logger.debug("federation target resolve failed for %s: %s", recipient, exc)
        return None

    def _dm_ratchet_manager(self):
        """Lazily build the 1:1 DM ratchet manager (RFC-0001 P1), or None.

        Gated OFF by default: returns a manager only when ``SKCHAT_DM_RATCHET`` is
        truthy AND a hybrid keypair is available. Cached (incl. the None result) so
        a disabled/unavailable build never re-pays the construction cost. Any error
        degrades to None → the classical path, never a send/receive failure.
        """
        cached = getattr(self, "_dm_mgr_cached", "unset")
        if cached != "unset":
            return cached
        mgr = None
        flag = os.getenv("SKCHAT_DM_RATCHET", "").strip().lower()
        if self._crypto and flag not in ("", "0", "false", "no", "off"):
            try:
                from pathlib import Path

                from .dm_manager import DmRatchetManager

                agent = (self._identity or "").split(":")[-1].split("@")[0] or "lumina"
                # Co-locate the DM-session store with the prekey store: both honor
                # SKCHAT_HOME (pq_prekeys uses it for ~/.skchat/pqc). Identical to
                # the previous hard-coded path in production (SKCHAT_HOME unset →
                # ~/.skchat), but keeps the at-rest store-key (derived from the
                # SKCHAT_HOME-scoped hybrid key) and the session DB on one tree.
                home = Path(os.environ.get("SKCHAT_HOME") or os.path.expanduser("~/.skchat"))
                store_dir = home / "pqc"
                store_dir.mkdir(parents=True, exist_ok=True)
                mgr = DmRatchetManager.for_agent(self._crypto, agent, store_dir)
            except Exception as exc:
                logger.warning("DM ratchet manager unavailable (classical fallback): %s", exc)
                mgr = None
        self._dm_mgr_cached = mgr
        return mgr

    def _maybe_fetch_remote_prekey(self, peer: str) -> bool:
        """First-contact: pull a federated peer's pqdr1 prekey over S2S, once.

        Wires :func:`prekey_exchange.fetch_peer_prekey` into the live path so a
        REMOTE peer (e.g. ``jarvis@<op>.<realm>`` on another node) — who never
        lands in our local prekey store — can be ratcheted cross-node. Attempted
        only when ALL of:

          * the DM ratchet is enabled (``SKCHAT_DM_RATCHET`` — surfaced as a live
            :class:`DmRatchetManager`; OFF ⇒ no fetch at all), AND
          * we do NOT already have a ratchet-capable bundle for the peer, AND
          * the peer resolves to a reachable remote node (``https-s2s`` inbox).

        Downgrade-safe: an unroutable peer, a bundle without the ``pqdr1``
        capability, or any error ⇒ stays classical, never raises. The pull is
        one-shot per process (``_prekey_fetch_attempted``) so a classical /
        unreachable peer is not re-fetched on every message.

        Returns:
            bool: True iff a ratchet-capable bundle is now stored locally.
        """
        mgr = self._dm_ratchet_manager()
        if mgr is None:
            return False  # ratchet disabled / no local hybrid keypair → classical
        # Already resolvable as a ratchet-capable peer? nothing to fetch.
        try:
            if mgr.can_ratchet(peer):
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("can_ratchet(%s) probe failed: %s", peer, exc)
            return False

        norm = peer.split(":", 1)[1] if peer.startswith("capauth:") else peer
        short = norm.split("@", 1)[0]
        if short in self._prekey_fetch_attempted:
            return False  # one-shot per process — don't hammer a classical peer

        fqid = self._federation_target(peer)
        if not fqid:
            return False  # not a reachable remote node → classical (no fetch)

        self._prekey_fetch_attempted.add(short)
        try:
            from . import prekey_exchange

            kwargs: dict = {"http_get": self._prekey_http_get}
            if self._prekey_inbox_resolver is not None:
                kwargs["inbox_resolver"] = self._prekey_inbox_resolver
            stored = prekey_exchange.fetch_peer_prekey(fqid, **kwargs)
            if prekey_exchange.is_ratchet_capable(stored):
                logger.info("Fetched pqdr1 prekey for remote peer %s (cross-node ratchet)", short)
                return True
            return False
        except Exception as exc:  # noqa: BLE001
            logger.debug("remote prekey fetch for %s failed (classical fallback): %s", peer, exc)
            return False

    def send_message(
        self,
        message: ChatMessage,
        recipient_public_armor: Optional[str] = None,
    ) -> dict:
        """Send a ChatMessage via SKComms.

        Encrypts (if crypto and public key are available), serializes
        the ChatMessage into the SKComms envelope payload, and routes
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
                # RFC-0001 P1: prefer the Level-3 DM ratchet when enabled + the peer
                # has a hybrid prekey (env SKCHAT_DM_RATCHET). Ratchet bodies are
                # AEAD-authenticated and intentionally UNSIGNED (deniable). Any miss
                # falls back to the unchanged classical PGP path.
                mgr = self._dm_ratchet_manager()
                if mgr is not None and not mgr.can_ratchet(message.recipient):
                    # First-contact: pull a federated peer's pqdr1 prekey so this
                    # cross-node DM can ratchet. No-op when already resolvable /
                    # not a remote node — downgrade-safe (classical fallback).
                    self._maybe_fetch_remote_prekey(message.recipient)
                sealed = (
                    mgr.seal(outbound)
                    if mgr is not None and mgr.can_ratchet(message.recipient)
                    else outbound
                )
                if self._crypto.is_ratchet_message(sealed):
                    outbound = sealed  # ratchet-sealed — deniable, no PGP signature
                else:
                    outbound = self._crypto.encrypt_message(
                        outbound, recipient_public_armor
                    )
                    outbound = self._crypto.sign_message(outbound)
            except Exception as exc:
                logger.warning("Encryption failed, sending plaintext: %s", exc)

        payload_json = outbound.model_dump_json()

        # For local same-machine peers, write a loopback copy to the file-transport
        # outbox so poll_outbox_for_lumina() (and equivalent pollers) can pick up the
        # message immediately without waiting for the SQLite history backlog.
        if message.recipient in _LOCAL_PEERS:
            _write_local_loopback(message)

        # Loopback: when sender == receiver, write directly to own per-fingerprint
        # inbox so the polling loop detects the message on the next cycle.
        if message.recipient == self._identity:
            self._write_file_inbox(message, payload_json)
            stored_msg = message.model_copy(update={"delivery_status": DeliveryStatus.SENT})
            self._history.store_message(stored_msg)
            return {
                "delivered": True,
                "message_id": message.id,
                "recipient": message.recipient,
                "transport": "file",
            }

        # Federation (SKFed): if the recipient is a reachable remote peer (has an
        # https-s2s inbox_url in the skcomms peer store), deliver node-to-node via
        # the canonical signed S2S path instead of the legacy local transports.
        fed_fqid = self._federation_target(message.recipient)
        if fed_fqid is not None and hasattr(self._skcomms, "send_federated"):
            try:
                report = self._skcomms.send_federated(
                    fed_fqid,
                    payload_json,
                    thread_id=message.thread_id,
                    in_reply_to=message.reply_to_id,
                )
                delivered = getattr(report, "delivered", False)
                if delivered:
                    self._history.store_message(
                        message.model_copy(update={"delivery_status": DeliveryStatus.SENT})
                    )
                    return {
                        "delivered": True,
                        "message_id": message.id,
                        "recipient": message.recipient,
                        "transport": "skfed-s2s",
                    }
                logger.info("federated send to %s not delivered — falling back", fed_fqid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("federated send to %s failed (%s) — falling back", fed_fqid, exc)

        try:
            report = self._skcomms.send(
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
            logger.error("SKComms send failed: %s", exc)

            # Try fallback transport if primary failed
            if self._fallback_transport is not None:
                try:
                    fallback_report = self._fallback_transport.send(
                        recipient=message.recipient,
                        message=payload_json,
                    )
                    fallback_delivered = getattr(fallback_report, "delivered", True)
                    stored_msg = message.model_copy(
                        update={
                            "delivery_status": (
                                DeliveryStatus.SENT
                                if fallback_delivered
                                else DeliveryStatus.FAILED
                            ),
                        }
                    )
                    self._history.store_message(stored_msg)
                    return {
                        "delivered": fallback_delivered,
                        "message_id": message.id,
                        "recipient": message.recipient,
                        "transport": "file",
                    }
                except Exception as fb_exc:
                    logger.error("Fallback transport also failed: %s", fb_exc)

            failed_msg = message.model_copy(update={"delivery_status": DeliveryStatus.FAILED})
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
        """Poll SKComms for incoming messages and store them.

        Receives all pending envelopes from SKComms, extracts
        ChatMessage payloads, optionally decrypts them, stores
        in ChatHistory, and returns the messages.

        Args:
            sender_public_armor: Optional PGP public key for
                signature verification on incoming messages.

        Returns:
            list[ChatMessage]: Newly received ChatMessages.
        """
        try:
            envelopes = self._skcomms.receive()
        except Exception as exc:
            logger.error("SKComms receive failed: %s", exc)
            return []

        messages: list[ChatMessage] = []

        for envelope in envelopes:
            try:
                # Route HEARTBEAT envelopes to presence/typing handler
                try:
                    from skcomms.models import MessageType as _MsgType

                    if getattr(envelope, "message_type", None) == _MsgType.HEARTBEAT:
                        self._handle_heartbeat(envelope)
                        continue
                except ImportError:
                    pass

                payload_content = self._extract_payload(envelope)
                if payload_content is None:
                    continue

                # Resolve the envelope-level sender (PGP fingerprint or agent name)
                # to a canonical CapAuth URI so it can be used as a fallback when
                # the inner ChatMessage payload has a missing or bare sender field.
                envelope_sender = getattr(envelope, "sender", "") or ""
                if envelope_sender and not envelope_sender.startswith("capauth:"):
                    try:
                        from .peer_discovery import PeerDiscovery as _PD

                        _peer = _PD().get_peer(envelope_sender)
                        if _peer:
                            for _uri in _peer.get("contact_uris", []):
                                if _uri.startswith("capauth:") and "@" in _uri:
                                    envelope_sender = _uri
                                    break
                    except Exception as e:
                        logger.warning("transport.py: %s", e)
                        pass

                try:
                    msg = ChatMessage.model_validate_json(payload_content)
                except Exception as e:
                    logger.warning("transport.py: %s", e)
                    # Payload is not a ChatMessage JSON — wrap plain text using
                    # the envelope sender so the message is not silently dropped.
                    if not envelope_sender:
                        logger.debug("Skipping envelope: not a ChatMessage and no envelope sender")
                        continue
                    envelope_recipient = getattr(envelope, "recipient", "") or self._identity
                    if not envelope_recipient.startswith("capauth:"):
                        envelope_recipient = self._identity
                    try:
                        msg = ChatMessage(
                            sender=envelope_sender,
                            recipient=envelope_recipient,
                            content=str(payload_content)[:4096] or "(empty)",
                        )
                        logger.debug(
                            "Wrapped plain-text envelope from %s as ChatMessage",
                            envelope_sender,
                        )
                    except Exception as exc2:
                        logger.debug("Failed to wrap envelope as ChatMessage: %s", exc2)
                        continue

                # If the ChatMessage sender is a bare identifier (no scheme), try
                # to resolve it via the peer store to a full capauth: URI.
                if msg.sender and ":" not in msg.sender:
                    try:
                        from .peer_discovery import PeerDiscovery as _PD2

                        _p = _PD2().get_peer(msg.sender)
                        if _p:
                            for _u in _p.get("contact_uris", []):
                                if _u.startswith("capauth:") and "@" in _u:
                                    msg = msg.model_copy(update={"sender": _u})
                                    break
                    except Exception as e:
                        logger.warning("transport.py: %s", e)
                        pass

                # RFC-0001 P1: a pqdr1: ratchet body opens through the DM ratchet
                # (AEAD-authenticated, unsigned) — skip the classical decrypt/verify.
                _mgr = self._dm_ratchet_manager()
                if _mgr is not None and _mgr.can_open(msg):
                    try:
                        msg = _mgr.open(msg)
                    except Exception as exc:
                        logger.warning(
                            "Ratchet-decrypt failed for %s: %s", msg.id[:8], exc
                        )
                elif self._crypto and msg.encrypted:
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

                msg = msg.model_copy(update={"delivery_status": DeliveryStatus.DELIVERED})
                self._history.store_message(msg)
                messages.append(msg)

                # First-contact pre-warm: if this arrived from a reachable remote
                # node and the ratchet is on, pull its pqdr1 prekey so our REPLY
                # can ratchet cross-node. Best-effort + one-shot — never fatal.
                self._maybe_fetch_remote_prekey(msg.sender)

            except Exception as exc:
                logger.debug("Failed to process envelope: %s", exc)

        # Also poll the per-fingerprint file inbox directly, independent of
        # the SKComms receive path.  This catches messages written by peers on
        # the same machine (including loopback self-messages).
        file_messages = self._poll_file_inbox()
        messages.extend(file_messages)

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

    def _get_own_fingerprint(self) -> str:
        """Derive a filesystem-safe fingerprint/slug for the local agent.

        Reads the PGP fingerprint from ~/.skcomms/config.yml when available.
        Falls back to a sanitized slug derived from the identity URI so the
        per-fingerprint inbox path always resolves to a stable directory.

        Returns:
            str: PGP fingerprint (hex) or a sanitized identity slug.
        """
        try:
            import yaml  # soft dep — available in all SKComms environments

            config_path = Path("~/.skcomms/config.yml").expanduser()
            if config_path.exists():
                with open(config_path) as _f:
                    cfg = yaml.safe_load(_f) or {}
                fp = cfg.get("skcomms", {}).get("identity", {}).get("fingerprint", "")
                if fp:
                    return str(fp).replace(" ", "")
        except Exception as e:
            logger.warning("transport.py: %s", e)
            pass
        # Sanitize identity URI → filesystem-safe slug
        slug = (
            self._identity.replace("capauth:", "")
            .replace("@", "_at_")
            .replace(":", "_")
            .replace("/", "_")
            .replace(" ", "_")
        )
        return slug or "local"

    def _write_file_inbox(self, message: ChatMessage, payload_json: str) -> None:
        """Write a ChatMessage JSON envelope to the local per-fingerprint inbox.

        Used for loopback delivery (sender == receiver) so the polling loop
        picks up self-addressed messages on the next cycle.

        Args:
            message: The ChatMessage being delivered.
            payload_json: Serialized ChatMessage JSON string.
        """
        fingerprint = self._get_own_fingerprint()
        inbox_dir = self._file_inbox_root / fingerprint
        inbox_dir.mkdir(parents=True, exist_ok=True)

        envelope_id = uuid.uuid4().hex
        envelope = {
            "skcomms_version": "1.0.0",
            "envelope_id": envelope_id,
            "sender": message.sender,
            "recipient": message.recipient,
            "payload": {
                "content": payload_json,
                "content_type": "text",
                "encrypted": False,
                "compressed": False,
                "signature": None,
            },
            "routing": {
                "mode": "failover",
                "preferred_transports": [],
                "retry_max": 2,
                "retry_backoff": [5, 15, 60, 300, 900],
                "ttl": 86400,
                "ack_requested": False,
            },
            "metadata": {
                "thread_id": message.thread_id,
                "in_reply_to": message.reply_to_id,
                "urgency": "normal",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "expires_at": None,
                "attempt": 0,
                "delivered_via": "file_loopback",
            },
        }

        filename = f"{envelope_id}.skc.json"
        target = inbox_dir / filename
        tmp = inbox_dir / f".{filename}.tmp"
        try:
            tmp.write_bytes(json.dumps(envelope).encode("utf-8"))
            tmp.rename(target)
            logger.debug(
                "Loopback written to file inbox: %s → %s",
                envelope_id[:8],
                target,
            )
        except OSError as exc:
            logger.warning("File inbox loopback write failed: %s", exc)

    def _poll_file_inbox(self) -> list[ChatMessage]:
        """Scan the per-fingerprint file inbox directory for new messages.

        Reads ~/.skcomms/transport/file/inbox/<fingerprint>/*.skc.json,
        parses each file as a MessageEnvelope (or falls back to raw
        ChatMessage JSON), stores valid messages in history, and archives
        each processed file.

        Returns:
            list[ChatMessage]: Newly received and stored messages.
        """
        fingerprint = self._get_own_fingerprint()
        inbox_dir = self._file_inbox_root / fingerprint

        if not inbox_dir.exists():
            return []

        archive_dir = self._file_inbox_root / "archive" / fingerprint
        messages: list[ChatMessage] = []

        for env_file in sorted(inbox_dir.glob("*.skc.json")):
            if env_file.name.startswith("."):
                continue  # skip tmp files

            try:
                data = env_file.read_bytes()
            except OSError as exc:
                logger.warning("Cannot read file inbox entry %s: %s", env_file.name, exc)
                continue

            payload_content: Optional[str] = None

            envelope_sender_file: str = ""

            # Try full MessageEnvelope first (standard SKComms wire format)
            try:
                from skcomms.models import MessageEnvelope

                envelope = MessageEnvelope.from_bytes(data)
                payload_content = self._extract_payload(envelope)
                envelope_sender_file = getattr(envelope, "sender", "") or ""
            except Exception as e:
                logger.warning("transport.py: %s", e)
                pass

            # Fall back: parse raw JSON and unwrap payload.content or use as-is
            if payload_content is None:
                try:
                    raw = data.decode("utf-8")
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict) and "payload" in parsed:
                        inner = parsed["payload"]
                        if isinstance(inner, dict):
                            payload_content = inner.get("content")
                        else:
                            payload_content = str(inner)
                        if not envelope_sender_file:
                            envelope_sender_file = parsed.get("sender", "")
                    else:
                        payload_content = raw
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass

            if payload_content is None:
                logger.debug("No payload in file inbox entry %s — archiving", env_file.name)
                self._archive_file_inbox_entry(env_file, archive_dir)
                continue

            try:
                msg = ChatMessage.model_validate_json(payload_content)
                # Normalize bare sender (no scheme) to full capauth URI
                if msg.sender and ":" not in msg.sender:
                    try:
                        from .peer_discovery import PeerDiscovery as _PD3

                        _p3 = _PD3().get_peer(msg.sender)
                        if _p3:
                            for _u3 in _p3.get("contact_uris", []):
                                if _u3.startswith("capauth:") and "@" in _u3:
                                    msg = msg.model_copy(update={"sender": _u3})
                                    break
                    except Exception as e:
                        logger.warning("transport.py: %s", e)
                        pass
                msg = msg.model_copy(update={"delivery_status": DeliveryStatus.DELIVERED})
                self._history.store_message(msg)
                messages.append(msg)
            except Exception as exc:
                logger.debug(
                    "File inbox entry %s is not a valid ChatMessage: %s — trying envelope sender",
                    env_file.name,
                    exc,
                )
                # Wrap as ChatMessage using envelope sender when payload parsing fails
                if envelope_sender_file:
                    try:
                        from .peer_discovery import PeerDiscovery as _PD4

                        _env_sender = envelope_sender_file
                        if ":" not in _env_sender:
                            _p4 = _PD4().get_peer(_env_sender)
                            if _p4:
                                for _u4 in _p4.get("contact_uris", []):
                                    if _u4.startswith("capauth:") and "@" in _u4:
                                        _env_sender = _u4
                                        break
                        _fallback_msg = ChatMessage(
                            sender=_env_sender,
                            recipient=self._identity,
                            content=str(payload_content)[:4096] or "(empty)",
                        )
                        _fallback_msg = _fallback_msg.model_copy(
                            update={"delivery_status": DeliveryStatus.DELIVERED}
                        )
                        self._history.store_message(_fallback_msg)
                        messages.append(_fallback_msg)
                        logger.debug(
                            "Wrapped file inbox entry from %s as ChatMessage",
                            _env_sender,
                        )
                    except Exception as exc2:
                        logger.debug("Cannot wrap file inbox entry %s: %s", env_file.name, exc2)

            # Archive whether parse succeeded or failed (avoids re-processing)
            self._archive_file_inbox_entry(env_file, archive_dir)

        if messages:
            logger.info("File inbox: received %d message(s)", len(messages))

        return messages

    @staticmethod
    def _archive_file_inbox_entry(path: Path, archive_dir: Path) -> None:
        """Move a processed file inbox entry to the archive directory.

        Uses rename for atomic move; falls back to unlink on cross-device errors.

        Args:
            path: The .skc.json file to archive.
            archive_dir: Destination archive directory.
        """
        try:
            archive_dir.mkdir(parents=True, exist_ok=True)
            dest = archive_dir / path.name
            if dest.exists():
                dest = archive_dir / f"{int(time.time())}-{path.name}"
            path.rename(dest)
        except OSError:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _extract_payload(envelope: object) -> Optional[str]:
        """Extract the message content from an SKComms envelope.

        Handles both MessageEnvelope objects and raw dicts.

        Args:
            envelope: An SKComms MessageEnvelope or dict.

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
            from skcomms.models import MessageType

            self._skcomms.send(
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
            envelope: An SKComms MessageEnvelope with message_type=HEARTBEAT.
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
