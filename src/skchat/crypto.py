"""CapAuth encryption and signing wrappers for SKChat.

Wraps PGPy to provide message-level encryption and signing.
CapAuth handles identity (key generation, challenge-response);
this module handles the data-plane crypto: encrypt, decrypt, sign, verify.

All operations use PGPy directly because CapAuth's CryptoBackend
only exposes signing/verification, not encryption/decryption.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pgpy
from pgpy.constants import (
    HashAlgorithm,
    SymmetricKeyAlgorithm,
)

from .models import ChatMessage


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
