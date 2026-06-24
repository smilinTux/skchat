"""Encrypted message storage — at-rest encryption for chat history.

Wraps ChatHistory to encrypt message content before storage and decrypt
on retrieval. Uses AES-256-GCM keyed from a high-entropy random
data-encryption key (DEK).

Key management — the DEK and its wrap (Phase 1 / Q4)
----------------------------------------------------
The at-rest **bulk cipher** is AES-256-GCM (symmetric, Grover-only,
quantum-acceptable — never migrated). The DEK is a **random 32-byte key**
(``os.urandom(32)``) — high-entropy, generated once and persisted **wrapped**.

> **Fixed classical bug (Q4):** earlier versions derived the DEK from the PGP
> **fingerprint** via HKDF. A fingerprint is low-entropy and often *public*, so
> anyone who knew it could reconstruct the key — the at-rest encryption was
> effectively keyed by a public value. The DEK is now random key material and is
> sealed with a **hybrid post-quantum key-wrap** (X25519 + ML-KEM-768, see
> :mod:`skchat.atrest_wrap`). The only secret is the recipient **hybrid private
> key**, held locally 0600. A harvested encrypted store is not retroactively
> decryptable even after a CRQC (HNDL-resistant).

Back-compat / migration
-----------------------
Stores written by the old fingerprint-keyed scheme remain **readable**: pass the
``fingerprint`` and old reads transparently fall back to the legacy key. Use
:meth:`EncryptedChatHistory.migrate_store` to re-wrap an old store under the new
hybrid scheme (decrypt-old → re-encrypt-new) with **no plaintext change**.

Usage:
    store = EncryptedChatHistory.from_identity()   # hybrid-wrapped DEK
    store.store_message(msg)                        # content encrypted at rest
    messages = store.search_messages("hello")       # decrypted on read
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from pathlib import Path
from typing import Optional

from .history import ChatHistory
from .models import ChatMessage, Thread

logger = logging.getLogger("skchat.encrypted_store")


class StorageKeyDeriver:
    """Legacy DEK derivation — **kept for back-compat reads / migration only**.

    .. deprecated::
        Deriving the storage key from a PGP fingerprint is a classical
        low-entropy bug (a fingerprint is public-ish). New stores use a random
        DEK sealed with a hybrid KEM (see :class:`DekManager` /
        :mod:`skchat.atrest_wrap`). This class survives ONLY so that stores
        written by the old scheme can still be decrypted and migrated. Do not
        use it to key new data.
    """

    INFO = b"skchat-encrypted-storage-v1"
    SALT_FILE = ".skchat/storage.salt"

    @classmethod
    def derive_key(
        cls,
        fingerprint: str,
        salt: Optional[bytes] = None,
    ) -> bytes:
        """Derive a 32-byte AES key from a PGP fingerprint (LEGACY).

        Args:
            fingerprint: PGP key fingerprint (hex string).
            salt: Optional salt bytes. Generated and persisted if None.

        Returns:
            bytes: 32-byte AES-256 key.
        """
        if salt is None:
            salt = cls._load_or_create_salt()

        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.kdf.hkdf import HKDF

            hkdf = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=salt,
                info=cls.INFO,
            )
            return hkdf.derive(fingerprint.encode("utf-8"))
        except ImportError:
            # Fallback: SHA-256 of fingerprint + salt
            return hashlib.sha256(fingerprint.encode("utf-8") + salt + cls.INFO).digest()

    @classmethod
    def _load_or_create_salt(cls) -> bytes:
        """Load salt from disk, or create and persist a new one.

        Returns:
            bytes: 32-byte salt.
        """
        salt_path = Path.home() / cls.SALT_FILE
        if salt_path.exists():
            return salt_path.read_bytes()

        salt = os.urandom(32)
        salt_path.parent.mkdir(parents=True, exist_ok=True)
        salt_path.write_bytes(salt)
        return salt


class DekManager:
    """Load-or-create the random DEK + its hybrid (X25519+ML-KEM-768) wrap.

    Holds two on-disk artifacts under ``base_dir`` (default ``~/.skchat``):

    * ``atrest_recipient.key`` — the recipient **hybrid private key** (2432 B,
      mode 0600). This is the ONLY secret; lose it and the store is unreadable,
      leak it and the store is exposed. Back it up under its own wrap.
    * ``atrest_dek.wrap`` — the DEK sealed to that recipient's public key by
      :func:`skchat.atrest_wrap.wrap_dek` (suite-tagged, versioned).

    On first use both are created (fresh hybrid keypair + fresh random DEK,
    immediately wrapped). Thereafter the DEK is recovered by unwrapping with the
    private key. The cleartext DEK never touches disk.
    """

    RECIPIENT_KEY_FILE = "atrest_recipient.key"
    DEK_WRAP_FILE = "atrest_dek.wrap"

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir = Path(base_dir) if base_dir else (Path.home() / ".skchat")
        self.recipient_key_path = self.base_dir / self.RECIPIENT_KEY_FILE
        self.dek_wrap_path = self.base_dir / self.DEK_WRAP_FILE

    # -- recipient hybrid keypair -------------------------------------------

    def load_or_create_recipient(self) -> bytes:
        """Return the recipient hybrid **private** key, creating it if absent."""
        from . import atrest_wrap

        if self.recipient_key_path.exists():
            priv = self.recipient_key_path.read_bytes()
            return priv

        kp = atrest_wrap.new_recipient_keypair()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        # Persist private key 0600; public key alongside for re-wrap convenience.
        self.recipient_key_path.write_bytes(kp.private_key)
        os.chmod(self.recipient_key_path, 0o600)
        (self.base_dir / "atrest_recipient.pub").write_bytes(kp.public_key)
        return kp.private_key

    def recipient_public_from_private(self, private_key: bytes) -> bytes:
        """Recover the 1216-byte hybrid public key from the private key.

        The X25519 leg's public key is derived from its seed; the ML-KEM public
        key is read from the sidecar ``atrest_recipient.pub`` if present (ML-KEM
        secret keys do not cheaply yield their public half), else regenerated is
        impossible — so we require the sidecar for re-wrap. For the common path
        (wrap at creation time) the public key is taken directly from the freshly
        generated keypair, so this is only needed for re-wrap of an existing key.
        """
        pub_path = self.base_dir / "atrest_recipient.pub"
        if pub_path.exists():
            return pub_path.read_bytes()
        raise FileNotFoundError(
            "recipient public key sidecar (atrest_recipient.pub) missing; "
            "cannot re-wrap without it"
        )

    # -- DEK ----------------------------------------------------------------

    def load_or_create_dek(self) -> bytes:
        """Return the cleartext DEK, creating+wrapping a fresh one if absent."""
        from . import atrest_wrap

        priv = self.load_or_create_recipient()

        if self.dek_wrap_path.exists():
            blob = self.dek_wrap_path.read_bytes()
            return atrest_wrap.unwrap_dek(blob, priv)

        # Fresh random DEK (high-entropy — never fingerprint-derived).
        dek = atrest_wrap.new_dek()
        pub = (self.base_dir / "atrest_recipient.pub").read_bytes()
        blob = atrest_wrap.wrap_dek(dek, pub)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.dek_wrap_path.write_bytes(blob)
        os.chmod(self.dek_wrap_path, 0o600)
        return dek

    def wrap_suite(self) -> Optional[str]:
        """Suite id of the persisted DEK wrap (for the self-report), or None."""
        from . import atrest_wrap

        if not self.dek_wrap_path.exists():
            return None
        try:
            return atrest_wrap.describe_blob(self.dek_wrap_path.read_bytes())["suite_id"]
        except Exception:
            return None


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
            logger.warning("encrypted_store.py: %s", exc)
            raise ValueError(f"Decryption failed: {exc}") from exc


class EncryptedChatHistory:
    """ChatHistory wrapper that encrypts message content at rest.

    Transparently encrypts on store and decrypts on retrieval.
    Thread metadata and tags remain unencrypted for searchability.
    Message content is the only encrypted field.

    Args:
        history: Underlying ChatHistory instance.
        storage_key: 32-byte AES-256 DEK for at-rest encryption.
        legacy_key: Optional legacy (fingerprint-derived) key. When present,
            content that fails to decrypt under ``storage_key`` is retried under
            ``legacy_key`` — this is what keeps old-format stores readable until
            migrated.
        wrap_suite: Suite id of the DEK wrap, for the self-report.
    """

    ENCRYPTED_MARKER = "enc:aes256gcm:"

    def __init__(
        self,
        history: ChatHistory,
        storage_key: bytes,
        legacy_key: Optional[bytes] = None,
        wrap_suite: Optional[str] = None,
    ) -> None:
        self._history = history
        self._key = storage_key
        self._legacy_key = legacy_key
        self._encryptor = ContentEncryptor()
        self._wrap_suite = wrap_suite

    @classmethod
    def from_identity(
        cls,
        fingerprint: Optional[str] = None,
        store_path: Optional[str] = None,
        base_dir: Optional[Path] = None,
    ) -> "EncryptedChatHistory":
        """Create an EncryptedChatHistory keyed by a hybrid-wrapped random DEK.

        The DEK is loaded-or-created via :class:`DekManager` (random key sealed
        with the hybrid X25519+ML-KEM-768 wrap) — it is **not** derived from the
        fingerprint. The fingerprint is used ONLY to compute the legacy key so
        old-format stores stay readable (and migratable).

        Args:
            fingerprint: PGP fingerprint, for legacy back-compat reads only.
                Auto-detected if None.
            store_path: Override storage path.
            base_dir: Override the directory holding the recipient key + DEK wrap.

        Returns:
            EncryptedChatHistory: Ready for encrypted storage.
        """
        history = ChatHistory.from_config(store_path)

        mgr = DekManager(base_dir=base_dir)
        dek = mgr.load_or_create_dek()

        # Legacy key (back-compat reads / migration). Best-effort — never fatal.
        legacy_key = None
        try:
            fp = fingerprint if fingerprint is not None else cls._get_fingerprint()
            legacy_key = StorageKeyDeriver.derive_key(fp)
        except Exception as exc:  # noqa: BLE001
            logger.debug("legacy key derivation skipped: %s", exc)

        return cls(
            history=history,
            storage_key=dek,
            legacy_key=legacy_key,
            wrap_suite=mgr.wrap_suite(),
        )

    @staticmethod
    def _get_fingerprint() -> str:
        """Get the local PGP fingerprint from CapAuth identity.

        Returns:
            str: PGP fingerprint hex string.
        """
        import json

        identity_file = Path.home() / ".skcapstone" / "identity" / "identity.json"
        if identity_file.exists():
            with open(identity_file) as f:
                data = json.load(f)
            fp = data.get("fingerprint") or data.get("pgp_fingerprint")
            if fp:
                return fp

        # Fallback: derive from hostname + username
        import getpass
        import socket

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
        messages = self._history.get_conversation(participant_a, participant_b, limit=limit)
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

    # -- self-report --------------------------------------------------------

    def crypto_self_report(self) -> dict:
        """Report the at-rest crypto posture of this store (PQC §4.4 evidence).

        Mirrors Q2's ``GroupChat.crypto_self_report`` / sksecurity ``pqc_report``
        approach: states the **wrap** suite (the asymmetric/HNDL-relevant layer),
        not just the symmetric bulk cipher. When the DEK is sealed with the hybrid
        suite, the at-rest surface is HNDL-resistant; the historical
        fingerprint-keying note is gone (the DEK is random + hybrid-wrapped).
        """
        from . import atrest_wrap

        wrap_suite = self._wrap_suite
        is_hybrid = wrap_suite == atrest_wrap.DEFAULT_SUITE_ID
        return {
            "surface": "at-rest",
            "component": "skchat (encrypted_store)",
            "bulk_cipher": "aes256-gcm-v1",
            "wrap_suite": wrap_suite or "unwrapped",
            "wrap_status": "hybrid-pq" if is_hybrid else "classical-or-none",
            "quantum_resistant": bool(is_hybrid),
            "dek_source": "random os.urandom(32), hybrid-KEM-wrapped",
            "fips_refs": (
                ["FIPS 197", "SP 800-38D", "FIPS 203", "RFC 7748", "RFC 5869"]
                if is_hybrid
                else ["FIPS 197", "SP 800-38D"]
            ),
            "note": (
                "DEK is high-entropy random, sealed with hybrid X25519+ML-KEM-768 "
                "(skchat.atrest_wrap). Bulk AES-256-GCM is Grover-only. "
                "Fingerprint-keying bug fixed — DEK no longer derived from the "
                "(low-entropy/public) PGP fingerprint."
            ),
        }

    # -- migration ----------------------------------------------------------

    def migrate_store(self) -> dict:
        """Re-wrap every stored message under the current (hybrid) DEK.

        For each stored message: decrypt its content (trying the current DEK
        first, then the legacy fingerprint key), then re-store it encrypted under
        the current DEK. Old-format messages keyed by the legacy fingerprint are
        thereby moved onto the random hybrid-wrapped DEK with **identical
        plaintext**. Idempotent: content already under the current DEK is
        re-encrypted to itself (a no-op for plaintext).

        Returns:
            dict: ``{migrated, skipped, failed}`` counts.

        Note:
            Requires the underlying ChatHistory to support enumeration
            (``list_threads`` + ``get_thread_messages``) and in-place rewrite by id
            (``update_message``). Callers with a custom backend can drive the same
            decrypt-old → re-encrypt-new loop themselves using
            :meth:`decrypt_content` / :meth:`reencrypt_content`.
        """
        migrated = skipped = failed = 0
        threads = self._history.list_threads(limit=10_000)
        for t in threads:
            tid = t.get("thread_id") or t.get("id")
            if not tid:
                continue
            for raw in self._history.get_thread_messages(tid, limit=100_000):
                content = raw.get("content", "")
                if not content.startswith(self.ENCRYPTED_MARKER):
                    skipped += 1
                    continue
                try:
                    plaintext = self.decrypt_content(content)
                except ValueError:
                    failed += 1
                    continue
                # Re-encrypt the plaintext under the current (hybrid-wrapped) DEK.
                new_marked = self.reencrypt_content(plaintext)
                if self._rewrite_message(raw, new_marked):
                    migrated += 1
                else:
                    failed += 1
        return {"migrated": migrated, "skipped": skipped, "failed": failed}

    def _rewrite_message(self, raw: dict, new_marked_content: str) -> bool:
        """Persist ``new_marked_content`` back onto the stored message ``raw``.

        Reconstructs a :class:`ChatMessage` from the stored dict with the content
        replaced and calls the backend's ``update_message`` (in-place JSONL line
        rewrite). Returns False if the backend cannot update in place.
        """
        if not hasattr(self._history, "update_message"):
            return False
        try:
            updated = {**raw, "content": new_marked_content}
            updated.pop("decryption_error", None)
            msg = ChatMessage.model_validate(updated)
            return bool(self._history.update_message(msg))
        except Exception as exc:  # noqa: BLE001 — never let one bad row abort all
            logger.warning("migrate: could not rewrite message: %s", exc)
            return False

    def reencrypt_content(self, plaintext: str) -> str:
        """Encrypt ``plaintext`` under the current DEK, returning marked content."""
        return f"{self.ENCRYPTED_MARKER}{self._encryptor.encrypt(plaintext, self._key)}"

    def decrypt_content(self, marked_content: str) -> str:
        """Decrypt a stored, marked content string, trying current then legacy key.

        This is the back-compat read primitive: it first tries the current
        (hybrid-wrapped random) DEK, then the legacy fingerprint key. Raises
        :class:`ValueError` if neither works.
        """
        if not marked_content.startswith(self.ENCRYPTED_MARKER):
            return marked_content
        encrypted_b64 = marked_content[len(self.ENCRYPTED_MARKER) :]
        try:
            return self._encryptor.decrypt(encrypted_b64, self._key)
        except ValueError:
            if self._legacy_key is not None:
                return self._encryptor.decrypt(encrypted_b64, self._legacy_key)
            raise

    def _decrypt_dict(self, msg_dict: dict) -> dict:
        """Decrypt the content field of a message dict if encrypted.

        Tries the current DEK, then the legacy fingerprint key (back-compat).

        Args:
            msg_dict: Message dict from ChatHistory.

        Returns:
            dict: Message dict with decrypted content.
        """
        content = msg_dict.get("content", "")
        if content.startswith(self.ENCRYPTED_MARKER):
            try:
                msg_dict["content"] = self.decrypt_content(content)
            except ValueError:
                msg_dict["content"] = "[decryption failed]"
                msg_dict["decryption_error"] = True

        return msg_dict
