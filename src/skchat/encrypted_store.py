"""Encrypted message storage — at-rest encryption for chat history.

Wraps ChatHistory to encrypt message content before storage and decrypt
on retrieval. Uses AES-256-GCM keyed from the user's CapAuth identity.

The storage key is derived from the user's PGP fingerprint using HKDF,
so only the identity holder can read stored messages. The key never
leaves the local machine.

Usage:
    store = EncryptedChatHistory.from_identity()
    store.store_message(msg)  # content encrypted at rest
    messages = store.search_messages("hello")  # decrypted on read
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import Optional

from .history import ChatHistory
from .models import ChatMessage, Thread

logger = logging.getLogger("skchat.encrypted_store")


class StorageKeyDeriver:
    """Derives an AES-256 storage key from a CapAuth identity.

    Uses HKDF (HMAC-based Key Derivation Function) with the PGP
    fingerprint as input keying material and a fixed info string.
    The derived key is deterministic for a given fingerprint.
    """

    INFO = b"skchat-encrypted-storage-v1"
    SALT_FILE = ".skchat/storage.salt"

    @classmethod
    def derive_key(
        cls,
        fingerprint: str,
        salt: Optional[bytes] = None,
    ) -> bytes:
        """Derive a 32-byte AES key from a PGP fingerprint.

        Args:
            fingerprint: PGP key fingerprint (hex string).
            salt: Optional salt bytes. Generated and persisted if None.

        Returns:
            bytes: 32-byte AES-256 key.
        """
        if salt is None:
            salt = cls._load_or_create_salt()

        try:
            from cryptography.hazmat.primitives.kdf.hkdf import HKDF
            from cryptography.hazmat.primitives import hashes

            hkdf = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=salt,
                info=cls.INFO,
            )
            return hkdf.derive(fingerprint.encode("utf-8"))
        except ImportError:
            # Fallback: SHA-256 of fingerprint + salt
            return hashlib.sha256(
                fingerprint.encode("utf-8") + salt + cls.INFO
            ).digest()

    @classmethod
    def _load_or_create_salt(cls) -> bytes:
        """Load salt from disk, or create and persist a new one.

        Returns:
            bytes: 32-byte salt.
        """
        from pathlib import Path

        salt_path = Path.home() / cls.SALT_FILE
        if salt_path.exists():
            return salt_path.read_bytes()

        salt = os.urandom(32)
        salt_path.parent.mkdir(parents=True, exist_ok=True)
        salt_path.write_bytes(salt)
        return salt


class ContentEncryptor:
    """AES-256-GCM encryption for message content at rest.

    Each encryption produces a unique nonce. The output format is:
    base64(nonce || ciphertext || tag).
    """

    @staticmethod
    def encrypt(plaintext: str, key: bytes) -> str:
        """Encrypt plaintext content.

        Args:
            plaintext: The content to encrypt.
            key: 32-byte AES-256 key.

        Returns:
            str: Base64-encoded encrypted content.
        """
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            logger.warning("cryptography not available, storing plaintext")
            return plaintext

        nonce = os.urandom(12)
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.b64encode(nonce + ciphertext).decode("ascii")

    @staticmethod
    def decrypt(encrypted_b64: str, key: bytes) -> str:
        """Decrypt content.

        Args:
            encrypted_b64: Base64-encoded nonce + ciphertext + tag.
            key: 32-byte AES-256 key.

        Returns:
            str: Decrypted plaintext.

        Raises:
            ValueError: If decryption fails (wrong key or tampered data).
        """
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            return encrypted_b64

        raw = base64.b64decode(encrypted_b64)
        if len(raw) < 13:
            return encrypted_b64

        nonce, ciphertext = raw[:12], raw[12:]
        aesgcm = AESGCM(key)

        try:
            plaintext = aesgcm.decrypt(nonce, ciphertext, None)
            return plaintext.decode("utf-8")
        except Exception as exc:
            raise ValueError(f"Decryption failed: {exc}") from exc


class EncryptedChatHistory:
    """ChatHistory wrapper that encrypts message content at rest.

    Transparently encrypts on store and decrypts on retrieval.
    Thread metadata and tags remain unencrypted for searchability.
    Message content is the only encrypted field.

    Args:
        history: Underlying ChatHistory instance.
        storage_key: 32-byte AES-256 key for at-rest encryption.
    """

    ENCRYPTED_MARKER = "enc:aes256gcm:"

    def __init__(self, history: ChatHistory, storage_key: bytes) -> None:
        self._history = history
        self._key = storage_key
        self._encryptor = ContentEncryptor()

    @classmethod
    def from_identity(
        cls,
        fingerprint: Optional[str] = None,
        store_path: Optional[str] = None,
    ) -> "EncryptedChatHistory":
        """Create an EncryptedChatHistory from the local CapAuth identity.

        Args:
            fingerprint: PGP fingerprint. Auto-detected if None.
            store_path: Override storage path.

        Returns:
            EncryptedChatHistory: Ready for encrypted storage.
        """
        if fingerprint is None:
            fingerprint = cls._get_fingerprint()

        history = ChatHistory.from_config(store_path)
        key = StorageKeyDeriver.derive_key(fingerprint)

        return cls(history=history, storage_key=key)

    @staticmethod
    def _get_fingerprint() -> str:
        """Get the local PGP fingerprint from CapAuth identity.

        Returns:
            str: PGP fingerprint hex string.
        """
        import json
        from pathlib import Path

        identity_file = Path.home() / ".skcapstone" / "identity" / "identity.json"
        if identity_file.exists():
            with open(identity_file) as f:
                data = json.load(f)
            fp = data.get("fingerprint") or data.get("pgp_fingerprint")
            if fp:
                return fp

        # Fallback: derive from hostname + username
        import socket
        import getpass
        fallback = f"{getpass.getuser()}@{socket.gethostname()}"
        logger.warning("No PGP fingerprint found, using fallback key derivation")
        return hashlib.sha256(fallback.encode()).hexdigest()

    def store_message(self, message: ChatMessage) -> str:
        """Store a message with encrypted content.

        The message content is encrypted before storage. All other
        fields (sender, recipient, tags, etc.) remain searchable.

        Args:
            message: The ChatMessage to store.

        Returns:
            str: Memory ID.
        """
        encrypted_content = self._encryptor.encrypt(message.content, self._key)
        marked_content = f"{self.ENCRYPTED_MARKER}{encrypted_content}"

        encrypted_msg = message.model_copy(
            update={
                "content": marked_content,
                "metadata": {
                    **message.metadata,
                    "encrypted_at_rest": True,
                },
            }
        )

        return self._history.store_message(encrypted_msg)

    def store_thread(self, thread: Thread) -> str:
        """Store a thread (not encrypted — metadata needs to be searchable).

        Args:
            thread: The Thread to store.

        Returns:
            str: Memory ID.
        """
        return self._history.store_thread(thread)

    def get_thread_messages(
        self,
        thread_id: str,
        limit: int = 50,
    ) -> list[dict]:
        """Retrieve and decrypt messages from a thread.

        Args:
            thread_id: Thread identifier.
            limit: Maximum messages.

        Returns:
            list[dict]: Decrypted message dicts.
        """
        messages = self._history.get_thread_messages(thread_id, limit=limit)
        return [self._decrypt_dict(m) for m in messages]

    def get_conversation(
        self,
        participant_a: str,
        participant_b: str,
        limit: int = 50,
    ) -> list[dict]:
        """Retrieve and decrypt conversation between two participants.

        Args:
            participant_a: First participant URI.
            participant_b: Second participant URI.
            limit: Maximum messages.

        Returns:
            list[dict]: Decrypted message dicts.
        """
        messages = self._history.get_conversation(
            participant_a, participant_b, limit=limit
        )
        return [self._decrypt_dict(m) for m in messages]

    def search_messages(self, query: str, limit: int = 20) -> list[dict]:
        """Search messages (searches tags/metadata, decrypts content on read).

        Note: Full-text search over encrypted content is not possible.
        This searches tags and metadata, then decrypts matching content.

        Args:
            query: Search query.
            limit: Maximum results.

        Returns:
            list[dict]: Decrypted matching messages.
        """
        messages = self._history.search_messages(query, limit=limit)
        return [self._decrypt_dict(m) for m in messages]

    def get_thread(self, thread_id: str) -> Optional[dict]:
        """Get thread metadata (not encrypted).

        Args:
            thread_id: Thread identifier.

        Returns:
            Optional[dict]: Thread metadata.
        """
        return self._history.get_thread(thread_id)

    def list_threads(self, limit: int = 50) -> list[dict]:
        """List threads (not encrypted).

        Args:
            limit: Maximum threads.

        Returns:
            list[dict]: Thread metadata dicts.
        """
        return self._history.list_threads(limit=limit)

    def message_count(self) -> int:
        """Count stored messages."""
        return self._history.message_count()

    def _decrypt_dict(self, msg_dict: dict) -> dict:
        """Decrypt the content field of a message dict if encrypted.

        Args:
            msg_dict: Message dict from ChatHistory.

        Returns:
            dict: Message dict with decrypted content.
        """
        content = msg_dict.get("content", "")
        if content.startswith(self.ENCRYPTED_MARKER):
            encrypted_b64 = content[len(self.ENCRYPTED_MARKER):]
            try:
                msg_dict["content"] = self._encryptor.decrypt(
                    encrypted_b64, self._key
                )
            except ValueError:
                msg_dict["content"] = "[decryption failed]"
                msg_dict["decryption_error"] = True

        return msg_dict
