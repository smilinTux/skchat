"""CapAuth encryption and signing wrappers for SKChat.

Wraps PGPy to provide message-level encryption and signing.
CapAuth handles identity (key generation, challenge-response);
this module handles the data-plane crypto: encrypt, decrypt, sign, verify.

All operations use PGPy directly because CapAuth's CryptoBackend
only exposes signing/verification, not encryption/decryption.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pgpy
from pgpy.constants import (
    SymmetricKeyAlgorithm,
)

from .models import ChatMessage

logger = logging.getLogger(__name__)

#: Wire marker for a hybrid-PQ sealed DM body stored in ``ChatMessage.content``.
#: Classical PGP content starts with ``-----BEGIN PGP``; hybrid content starts
#: with this prefix so both coexist in the same field (no model change, full
#: back-compat: classical messages are byte-for-byte unchanged).
PQDM_SCHEME = "pqdm1:"

# Default peer store location — mirrors skcapstone's convention
SKCAPSTONE_PEERS_DIR = Path.home() / ".skcapstone" / "peers"


class CryptoError(Exception):
    """Base exception for SKChat crypto operations."""


class EncryptionError(CryptoError):
    """Raised when PGP encryption fails."""


class DecryptionError(CryptoError):
    """Raised when PGP decryption fails."""


class SigningError(CryptoError):
    """Raised when PGP signing fails."""


class VerificationError(CryptoError):
    """Raised when PGP signature verification fails."""


@dataclass
class CryptoResult:
    """Result of a crypto operation on a ChatMessage.

    Attributes:
        message: The transformed ChatMessage.
        fingerprint: PGP fingerprint used in the operation.
        ok: Whether the operation succeeded.
    """

    message: ChatMessage
    fingerprint: str
    ok: bool


def _load_peer_public_key(
    peer_handle: str,
    peers_dir: Optional[Path] = None,
) -> str:
    """Load an ASCII-armored public key from the skcapstone peer store.

    Resolves the peer handle to ``<peers_dir>/<local>.json`` and returns
    the ``public_key`` field.

    Args:
        peer_handle: Peer identifier — any of: short name (``"alice"``),
            full handle (``"alice@skworld.io"``), or CapAuth URI
            (``"capauth:alice@skworld.io"``).
        peers_dir: Override the default ``~/.skcapstone/peers/`` directory.

    Returns:
        str: ASCII-armored PGP public key.

    Raises:
        CryptoError: If the peer file is missing or contains no ``public_key``.
    """
    store_dir = peers_dir or SKCAPSTONE_PEERS_DIR

    # Normalise: strip "capauth:" prefix, then take local part before "@"
    handle = peer_handle
    if ":" in handle:
        handle = handle.split(":", 1)[1]
    local = handle.split("@")[0].lower()

    peer_file = store_dir / f"{local}.json"
    if not peer_file.exists():
        raise CryptoError(f"Peer file not found: {peer_file}")

    try:
        with open(peer_file) as fh:
            record = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise CryptoError(f"Failed to read peer file {peer_file}: {exc}") from exc

    public_key = record.get("public_key", "")
    if not public_key:
        raise CryptoError(f"No public_key in peer record: {peer_file}")

    return public_key


class ChatCrypto:
    """PGP encryption and signing engine for chat messages.

    Uses PGPy for all operations. Expects ASCII-armored keys
    from CapAuth sovereign profiles.

    Args:
        private_key_armor: ASCII-armored PGP private key.
        passphrase: Passphrase to unlock the private key.
    """

    def __init__(self, private_key_armor: str, passphrase: str) -> None:
        try:
            self._private_key, _ = pgpy.PGPKey.from_blob(private_key_armor)
            self._passphrase = passphrase
            self._fingerprint = str(self._private_key.fingerprint).replace(" ", "")
        except Exception as exc:
            logger.warning("crypto.py: %s", exc)
            raise CryptoError(f"Failed to load private key: {exc}") from exc

    @property
    def fingerprint(self) -> str:
        """The fingerprint of the loaded private key.

        Returns:
            str: 40-character hex PGP fingerprint.
        """
        return self._fingerprint

    def encrypt_message(
        self,
        message: ChatMessage,
        recipient_public_armor: str,
    ) -> ChatMessage:
        """Encrypt a ChatMessage's content for a specific recipient.

        Encrypts the plaintext content with the recipient's public key
        and signs it with our private key. Sets the encrypted flag.

        Args:
            message: The ChatMessage with plaintext content.
            recipient_public_armor: ASCII-armored public key of the recipient.

        Returns:
            ChatMessage: A copy with encrypted content and signature.

        Raises:
            EncryptionError: If encryption fails.
        """
        if message.encrypted:
            return message

        try:
            recipient_key, _ = pgpy.PGPKey.from_blob(recipient_public_armor)
            pgp_message = pgpy.PGPMessage.new(message.content.encode("utf-8"))

            # Reason: PGPy requires unlocking to access encryption subkeys
            # on the recipient side, but for encryption we only need the public key.
            # We encrypt to recipient's public key.
            encrypted = recipient_key.encrypt(
                pgp_message,
                cipher=SymmetricKeyAlgorithm.AES256,
            )

            with self._private_key.unlock(self._passphrase):
                sig = self._private_key.sign(pgp_message)

            encrypted_copy = message.model_copy(
                update={
                    "content": str(encrypted),
                    "encrypted": True,
                    "signature": str(sig),
                }
            )
            return encrypted_copy

        except Exception as exc:
            logger.warning("crypto.py: %s", exc)
            raise EncryptionError(f"Failed to encrypt message: {exc}") from exc

    def decrypt_message(self, message: ChatMessage) -> ChatMessage:
        """Decrypt a ChatMessage's content using our private key.

        Args:
            message: The ChatMessage with PGP-encrypted content.

        Returns:
            ChatMessage: A copy with decrypted plaintext content.

        Raises:
            DecryptionError: If decryption fails.
        """
        if not message.encrypted:
            return message

        try:
            pgp_message = pgpy.PGPMessage.from_blob(message.content)

            with self._private_key.unlock(self._passphrase):
                decrypted = self._private_key.decrypt(pgp_message)

            plaintext = decrypted.message
            if isinstance(plaintext, bytes):
                plaintext = plaintext.decode("utf-8")

            return message.model_copy(
                update={
                    "content": plaintext,
                    "encrypted": False,
                }
            )

        except Exception as exc:
            logger.warning("crypto.py: %s", exc)
            raise DecryptionError(f"Failed to decrypt message: {exc}") from exc

    def encrypt_for_peer(
        self,
        message: ChatMessage,
        peer_handle: str,
        peers_dir: Optional[Path] = None,
    ) -> ChatMessage:
        """Encrypt a ChatMessage for a peer, resolving their key from the peer store.

        Convenience wrapper around :meth:`encrypt_message` that looks up the
        recipient's public key from ``~/.skcapstone/peers/<handle>.json``.

        Args:
            message: The ChatMessage with plaintext content.
            peer_handle: Peer identifier (short name, handle, or CapAuth URI).
            peers_dir: Override the default ``~/.skcapstone/peers/`` directory.

        Returns:
            ChatMessage: Encrypted and signed copy of the message.

        Raises:
            CryptoError: If the peer file or public key cannot be found.
            EncryptionError: If PGP encryption fails.
        """
        recipient_public_armor = _load_peer_public_key(peer_handle, peers_dir)
        return self.encrypt_message(message, recipient_public_armor)

    def decrypt_from_peer(
        self,
        message: ChatMessage,
        sender_handle: Optional[str] = None,
        peers_dir: Optional[Path] = None,
    ) -> tuple[ChatMessage, bool]:
        """Decrypt a message and optionally verify the sender's signature.

        Decrypts using our private key. When *sender_handle* is provided, also
        looks up the sender's public key from the peer store and verifies the
        detached signature on the plaintext.

        Args:
            message: The ChatMessage with PGP-encrypted content.
            sender_handle: Optional peer identifier for signature verification.
                When ``None``, skips verification and returns ``True`` for
                *sig_ok*.
            peers_dir: Override the default ``~/.skcapstone/peers/`` directory.

        Returns:
            tuple[ChatMessage, bool]: ``(decrypted_message, sig_ok)``.
                *sig_ok* is ``True`` if no *sender_handle* was provided or
                the signature verified successfully.

        Raises:
            DecryptionError: If decryption fails.
        """
        decrypted = self.decrypt_message(message)

        if sender_handle is None:
            return decrypted, True

        try:
            sender_public_armor = _load_peer_public_key(sender_handle, peers_dir)
        except CryptoError:
            return decrypted, False

        sig_ok = self.verify_signature(decrypted, sender_public_armor)
        return decrypted, sig_ok

    # ------------------------------------------------------------------
    # PQC Q3 — hybrid post-quantum DM sealing (HNDL fix, opt-in/negotiated).
    #
    # Adds a NEGOTIATED hybrid-KEM path *alongside* the classical PGP path.
    # ``encrypt_message``/``decrypt_message`` are untouched -> classical peers are
    # byte-for-byte unchanged. Hybrid engages only when the recipient advertises
    # a hybrid prekey bundle (PQXDH-style). The negotiated suite is recorded in
    # ``ChatMessage.metadata["kem_suite"]`` for the per-conversation self-report.
    # ------------------------------------------------------------------

    @staticmethod
    def supports_hybrid() -> bool:
        """Whether this build can do hybrid sealing (liboqs reachable)."""
        try:
            from skcomms.pqkem import is_available

            return is_available()
        except Exception:
            return False

    def negotiated_suite(self, recipient_bundle) -> str:
        """Resolve the DM suite for a conversation with ``recipient_bundle``.

        Hybrid (``x25519-mlkem768``) only when this side supports hybrid AND the
        recipient advertises a hybrid prekey; else the classical suite
        (negotiated downgrade). Single honest gate for the self-report and
        :meth:`encrypt_message_auto`.

        Args:
            recipient_bundle: A ``skcomms.pqdm.PrekeyBundle`` (or dict / None).
        """
        from skcomms.pqdm import PrekeyBundle, negotiate_suite

        bundle = (
            recipient_bundle
            if isinstance(recipient_bundle, PrekeyBundle)
            else PrekeyBundle.from_dict(recipient_bundle)
        )
        return negotiate_suite(self.supports_hybrid(), bundle)

    def encrypt_message_hybrid(
        self,
        message: ChatMessage,
        recipient_bundle,
    ) -> ChatMessage:
        """Hybrid-seal a DM body to the recipient's hybrid prekey.

        Encapsulates to the hybrid prekey (X25519+ML-KEM-768), AES-256-GCM seals
        the body, binds the negotiated suite + the (sender, recipient) pair into
        the downgrade-lock AAD, and stores ``PQDM_SCHEME + suite : base64(sealed)``
        in ``content`` with ``encrypted=True``. The body is *also* signed with our
        classical identity key (sig migrates in Phase 2). ``metadata["kem_suite"]``
        records the negotiated suite for the self-report.

        Args:
            message: ChatMessage with plaintext content.
            recipient_bundle: The recipient's hybrid ``PrekeyBundle``.

        Returns:
            ChatMessage: Encrypted (hybrid) + signed copy.

        Raises:
            EncryptionError: if hybrid sealing fails.
        """
        from skcomms.pqdm import HYBRID_SUITE, PrekeyBundle, seal

        if message.encrypted:
            return message
        bundle = (
            recipient_bundle
            if isinstance(recipient_bundle, PrekeyBundle)
            else PrekeyBundle.from_dict(recipient_bundle)
        )
        try:
            sealed = seal(
                message.content.encode("utf-8"),
                bundle,
                sender=message.sender,
                recipient=message.recipient,
            )
            token = f"{PQDM_SCHEME}{HYBRID_SUITE}:" + base64.b64encode(sealed).decode(
                "ascii"
            )
            # Sign the plaintext (classical, Phase-2 will go hybrid).
            pgp_message = pgpy.PGPMessage.new(message.content.encode("utf-8"))
            with self._private_key.unlock(self._passphrase):
                sig = self._private_key.sign(pgp_message)
            meta = dict(message.metadata)
            meta["kem_suite"] = HYBRID_SUITE
            return message.model_copy(
                update={
                    "content": token,
                    "encrypted": True,
                    "signature": str(sig),
                    "metadata": meta,
                }
            )
        except Exception as exc:
            logger.warning("crypto.py: %s", exc)
            raise EncryptionError(f"Failed to hybrid-encrypt message: {exc}") from exc

    def encrypt_message_auto(
        self,
        message: ChatMessage,
        recipient_public_armor: str,
        recipient_bundle=None,
    ) -> tuple[ChatMessage, str]:
        """Encrypt honouring negotiation: hybrid if advertised, else classical.

        The crypto-agile entry point. Hybrid-seals when the recipient advertises a
        hybrid prekey AND this side supports hybrid (suite ``x25519-mlkem768``);
        otherwise the *unchanged* classical PGP path (``encrypt_message``) — a
        genuine negotiated downgrade, recorded honestly.

        Returns:
            ``(message, negotiated_suite)``.
        """
        from skcomms.pqdm import HYBRID_SUITE

        suite = self.negotiated_suite(recipient_bundle)
        if suite == HYBRID_SUITE:
            return self.encrypt_message_hybrid(message, recipient_bundle), suite
        msg = self.encrypt_message(message, recipient_public_armor)
        # Record the classical suite for the self-report (back-compat: a peer
        # that never sets metadata still works; this is additive).
        if msg.metadata.get("kem_suite") != suite:
            meta = dict(msg.metadata)
            meta["kem_suite"] = suite
            msg = msg.model_copy(update={"metadata": meta})
        return msg, suite

    @staticmethod
    def is_hybrid_message(message: ChatMessage) -> bool:
        """Whether a message carries a hybrid-PQ sealed body."""
        c = message.content or ""
        return bool(message.encrypted) and c.startswith(PQDM_SCHEME)

    def decrypt_message_hybrid(
        self,
        message: ChatMessage,
        hybrid_private: bytes,
    ) -> ChatMessage:
        """Open a hybrid-sealed DM with this agent's hybrid private key.

        Binds the carried suite + (sender, recipient) into the AAD on open; a
        downgrade/strip attempt fails to authenticate
        (:class:`~skcomms.pqdm.DowngradeDetected` -> ``DecryptionError``).

        Args:
            message: ChatMessage with a hybrid-sealed body.
            hybrid_private: This agent's 2432-byte hybrid private key.

        Returns:
            ChatMessage: Copy with decrypted plaintext content.

        Raises:
            DecryptionError: on malformed input or a detected downgrade/tamper.
        """
        from skcomms.pqdm import PqDmError, open_sealed

        c = message.content or ""
        if not c.startswith(PQDM_SCHEME):
            raise DecryptionError("not a hybrid-sealed message")
        rest = c[len(PQDM_SCHEME) :]
        suite, _, b64 = rest.partition(":")
        try:
            sealed = base64.b64decode(b64)
            plaintext = open_sealed(
                sealed,
                hybrid_private,
                sender=message.sender,
                recipient=message.recipient,
                expected_suite=suite,
            )
        except PqDmError as exc:
            raise DecryptionError(f"Failed to hybrid-decrypt message: {exc}") from exc
        except Exception as exc:
            logger.warning("crypto.py: %s", exc)
            raise DecryptionError(f"Failed to hybrid-decrypt message: {exc}") from exc
        return message.model_copy(
            update={"content": plaintext.decode("utf-8"), "encrypted": False}
        )

    # ------------------------------------------------------------------
    # Ratchet DM path — stateful per-epoch sealing (RFC-0001 P1).
    #
    # Drives a ``skchat.dm_session.DmSession`` (forward secrecy across epochs +
    # PQ rekey heal) instead of the hybrid *one-shot* seal. The sealed frame is
    # stored as a ``pqdr1:`` token in ``content`` (mirrors the ``pqdm1:`` shape).
    # Pure methods — no daemon/persistence wiring here.
    # ------------------------------------------------------------------

    @staticmethod
    def is_ratchet_message(message: ChatMessage) -> bool:
        """Whether a message carries a sealed DM *ratchet* frame (``pqdr1:``)."""
        from .dm_session import PQDR_SCHEME

        c = message.content or ""
        return c.startswith(PQDR_SCHEME)

    def encrypt_message_ratchet(
        self,
        message: ChatMessage,
        session,
        peer_hybrid_pub: bytes,
    ) -> ChatMessage:
        """Seal a DM through the per-epoch ratchet ``session`` to the peer.

        Calls ``session.seal(plaintext, peer_hybrid_pub)`` (which (re)keys the
        epoch as needed and rides the wrapped epoch secret on the first frame of
        each epoch), stores the frame as a ``pqdr1:`` token in ``content``, and
        records the hybrid KEM suite (X25519 + ML-KEM-768, FIPS 203 ML-KEM) +
        the ratchet mode in ``metadata``. Hybrid is secure if EITHER leg holds.

        Args:
            message: ChatMessage with plaintext content.
            session: The :class:`skchat.dm_session.DmSession` for this peer.
            peer_hybrid_pub: The peer's hybrid public key (KAM recipient).

        Returns:
            ChatMessage: Encrypted (ratchet) copy with a ``pqdr1:`` token body.

        Raises:
            EncryptionError: if ratchet sealing fails.
        """
        if message.encrypted:
            return message
        try:
            frame = session.seal(message.content.encode("utf-8"), peer_hybrid_pub)
            token = frame.to_token()
            meta = dict(message.metadata)
            meta["kem_suite"] = "x25519-mlkem768"
            meta["ratchet"] = "dm-epoch"
            return message.model_copy(
                update={
                    "content": token,
                    "encrypted": True,
                    "metadata": meta,
                }
            )
        except Exception as exc:
            logger.warning("crypto.py: %s", exc)
            raise EncryptionError(f"Failed to ratchet-encrypt message: {exc}") from exc

    def decrypt_message_ratchet(
        self,
        message: ChatMessage,
        session,
        my_hybrid_priv: bytes,
    ) -> ChatMessage:
        """Open a ratchet-sealed DM (``pqdr1:`` token) through ``session``.

        Parses the token back into a ``SealedDmFrame``, accepts its KAM if it
        opens a new epoch, and decrypts the frame for ``(epoch, index)``.

        Args:
            message: ChatMessage with a ``pqdr1:`` ratchet body.
            session: The :class:`skchat.dm_session.DmSession` for this peer.
            my_hybrid_priv: This agent's hybrid private key (unwraps the KAM).

        Returns:
            ChatMessage: Copy with decrypted plaintext content.

        Raises:
            DecryptionError: on a non-ratchet body, malformed token, or open failure.
        """
        from .dm_session import PQDR_SCHEME, SealedDmFrame

        c = message.content or ""
        if not c.startswith(PQDR_SCHEME):
            raise DecryptionError("not a ratchet-sealed message")
        try:
            frame = SealedDmFrame.from_token(c)
            plaintext = session.open(frame, my_hybrid_priv)
        except Exception as exc:
            logger.warning("crypto.py: %s", exc)
            raise DecryptionError(f"Failed to ratchet-decrypt message: {exc}") from exc
        return message.model_copy(
            update={"content": plaintext.decode("utf-8"), "encrypted": False}
        )

    def sign_message(self, message: ChatMessage) -> ChatMessage:
        """Sign a ChatMessage's content with our private key.

        Creates a detached PGP signature over the message content.
        Does not encrypt — use encrypt_message for that.

        Args:
            message: The ChatMessage to sign.

        Returns:
            ChatMessage: A copy with the signature field set.

        Raises:
            SigningError: If signing fails.
        """
        try:
            pgp_message = pgpy.PGPMessage.new(message.content.encode("utf-8"), cleartext=False)

            with self._private_key.unlock(self._passphrase):
                sig = self._private_key.sign(pgp_message)

            return message.model_copy(update={"signature": str(sig)})

        except Exception as exc:
            logger.warning("crypto.py: %s", exc)
            raise SigningError(f"Failed to sign message: {exc}") from exc

    @staticmethod
    def verify_signature(
        message: ChatMessage,
        sender_public_armor: str,
    ) -> bool:
        """Verify the PGP signature on a ChatMessage.

        Args:
            message: The ChatMessage with a signature to verify.
            sender_public_armor: ASCII-armored public key of the sender.

        Returns:
            bool: True if the signature is valid.
        """
        if not message.signature:
            return False

        try:
            pub_key, _ = pgpy.PGPKey.from_blob(sender_public_armor)
            sig = pgpy.PGPSignature.from_blob(message.signature)

            # Reason: PGPy verifies inline-signed messages, so we rebuild the
            # PGPMessage and attach the signature before calling verify.
            content_bytes = message.content.encode("utf-8")
            pgp_message = pgpy.PGPMessage.new(content_bytes, cleartext=False)
            pgp_message |= sig

            verification = pub_key.verify(pgp_message)
            return bool(verification)

        except Exception as e:
            logger.warning("crypto.py: %s", e)
            return False

    @staticmethod
    def fingerprint_from_armor(key_armor: str) -> Optional[str]:
        """Extract PGP fingerprint from an ASCII-armored key.

        Args:
            key_armor: ASCII-armored public or private key.

        Returns:
            Optional[str]: 40-char hex fingerprint, or None on failure.
        """
        try:
            key, _ = pgpy.PGPKey.from_blob(key_armor)
            return str(key.fingerprint).replace(" ", "")
        except Exception as e:
            logger.warning("crypto.py: %s", e)
            return None


def verify_message(
    message: ChatMessage,
    peers_dir: Optional[Path] = None,
) -> bool:
    """Verify a message's PGP signature using the sender's key from the peer store.

    Resolves ``message.sender`` to a peer JSON file in *peers_dir* (default:
    ``~/.skcapstone/peers/``), loads the ``public_key`` field, and performs
    full cryptographic verification via :meth:`ChatCrypto.verify_signature`.

    Args:
        message: The ChatMessage with a signature to verify.
        peers_dir: Override the default ``~/.skcapstone/peers/`` directory.

    Returns:
        bool: ``True`` if the signature is valid. ``False`` if the message has
        no signature, the peer file is missing, or verification fails.
    """
    if not message.signature:
        return False

    try:
        sender_public_armor = _load_peer_public_key(message.sender, peers_dir)
    except CryptoError:
        return False

    return ChatCrypto.verify_signature(message, sender_public_armor)


def verify_message_signature(message: ChatMessage) -> bool:
    """Structural check: is the message signature field present and parseable?

    Confirms the ``signature`` field is non-empty and contains a valid PGP
    signature blob.  This is a *structural* check — it does not perform
    cryptographic verification.  Use :meth:`ChatCrypto.verify_signature`
    with the sender's public key for full cryptographic verification.

    Args:
        message: The ChatMessage to inspect.

    Returns:
        bool: True if ``message.signature`` is present and parses as a
        valid PGP signature; False otherwise.
    """
    if not message.signature:
        return False
    try:
        pgpy.PGPSignature.from_blob(message.signature)
        return True
    except Exception as e:
        logger.warning("crypto.py: %s", e)
        return False


def encrypt_message_body(content: str, recipient_fingerprint: str) -> str:
    """Encrypt plaintext content for a recipient.

    Module-level convenience wrapper for one-shot content encryption without
    instantiating :class:`ChatCrypto`.  The *recipient_fingerprint* parameter
    accepts an ASCII-armored PGP public key (CapAuth convention: key blobs
    are addressed by their fingerprint identity).

    Args:
        content: Plaintext string to encrypt.
        recipient_fingerprint: ASCII-armored PGP public key of the recipient.

    Returns:
        str: ASCII-armored PGP encrypted message.

    Raises:
        EncryptionError: If the key cannot be parsed or encryption fails.
    """
    try:
        recipient_key, _ = pgpy.PGPKey.from_blob(recipient_fingerprint)
        pgp_message = pgpy.PGPMessage.new(content.encode("utf-8"))
        encrypted = recipient_key.encrypt(
            pgp_message,
            cipher=SymmetricKeyAlgorithm.AES256,
        )
        return str(encrypted)
    except Exception as exc:
        logger.warning("crypto.py: %s", exc)
        raise EncryptionError(f"Failed to encrypt content: {exc}") from exc


def decrypt_message_body(encrypted: str, private_key_path: str) -> str:
    """Decrypt a PGP-encrypted string using a private key file.

    Module-level convenience wrapper for one-shot decryption without
    instantiating :class:`ChatCrypto`.  The key file must be an
    ASCII-armored PGP private key with no passphrase protection; use
    :class:`ChatCrypto` directly for passphrase-protected keys.

    Args:
        encrypted: ASCII-armored PGP encrypted message.
        private_key_path: Filesystem path to an unprotected ASCII-armored
            PGP private key file.

    Returns:
        str: Decrypted plaintext.

    Raises:
        DecryptionError: If the key cannot be loaded or decryption fails.
    """
    try:
        with open(private_key_path) as fh:
            private_key_armor = fh.read()
        private_key, _ = pgpy.PGPKey.from_blob(private_key_armor)
        pgp_message = pgpy.PGPMessage.from_blob(encrypted)
        decrypted = private_key.decrypt(pgp_message)
        plaintext = decrypted.message
        if isinstance(plaintext, bytes):
            plaintext = plaintext.decode("utf-8")
        return plaintext
    except (EncryptionError, DecryptionError):
        raise
    except Exception as exc:
        logger.warning("crypto.py: %s", exc)
        raise DecryptionError(f"Failed to decrypt content: {exc}") from exc


def load_agent_crypto(identity: Optional[str] = None) -> Optional["ChatCrypto"]:
    """Best-effort load the running agent's :class:`ChatCrypto` from its CapAuth key.

    The live daemon/CLI/webui historically built :class:`~skchat.transport.ChatTransport`
    with ``crypto=None``, which left the 1:1 DM ratchet inert on the live path
    (:meth:`ChatTransport._dm_ratchet_manager` gates on a truthy ``crypto``) even with
    ``SKCHAT_DM_RATCHET=1``. This resolves the agent's per-agent CapAuth signing key
    (``~/.skcapstone/agents/<agent>/capauth/identity/private.asc`` — the same key
    skcomms' ``EnvelopeSigner`` uses, loaded with an empty passphrase) and returns a
    ready ``ChatCrypto`` so the live path can seal/open ratchet frames AND keep the
    classical sign/encrypt path working.

    Best-effort by design: any failure (no key file, unreadable, unexpected agent)
    returns ``None`` so the caller stays on the exact prior behaviour (no crypto →
    classical/skcomms-signed path), never a hard failure.

    Args:
        identity: The CapAuth identity URI (e.g. ``capauth:lumina@skworld.io``); the
            agent short-name is derived from it, falling back to ``$SKAGENT`` then
            ``lumina``.

    Returns:
        A :class:`ChatCrypto` for the agent, or ``None`` if it cannot be loaded.
    """
    try:
        agent = (identity or "").split(":")[-1].split("@")[0].strip()
        if not agent:
            agent = (os.environ.get("SKAGENT") or "lumina").strip()
        key_path = os.path.expanduser(
            f"~/.skcapstone/agents/{agent}/capauth/identity/private.asc"
        )
        if not os.path.isfile(key_path):
            logger.debug("load_agent_crypto: no key for agent %r at %s", agent, key_path)
            return None
        with open(key_path) as fh:
            armor = fh.read()
        crypto = ChatCrypto(armor, "")
        logger.info("load_agent_crypto: live crypto wired for %r (fp %s)", agent, crypto.fingerprint)
        return crypto
    except Exception as exc:  # noqa: BLE001 — best-effort, never break transport init
        logger.warning("load_agent_crypto failed (classical fallback): %s", exc)
        return None
