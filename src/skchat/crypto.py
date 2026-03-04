"""CapAuth encryption and signing wrappers for SKChat.

Wraps PGPy to provide message-level encryption and signing.
CapAuth handles identity (key generation, challenge-response);
this module handles the data-plane crypto: encrypt, decrypt, sign, verify.

All operations use PGPy directly because CapAuth's CryptoBackend
only exposes signing/verification, not encryption/decryption.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pgpy
from pgpy.constants import (
    HashAlgorithm,
    SymmetricKeyAlgorithm,
)

from .models import ChatMessage

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
            pgp_message = pgpy.PGPMessage.new(
                message.content.encode("utf-8"), cleartext=False
            )

            with self._private_key.unlock(self._passphrase):
                sig = self._private_key.sign(pgp_message)

            return message.model_copy(update={"signature": str(sig)})

        except Exception as exc:
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

        except Exception:
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
        except Exception:
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
    except Exception:
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
        raise DecryptionError(f"Failed to decrypt content: {exc}") from exc
