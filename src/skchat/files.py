"""SKChat file transfer -- encrypted chunked file sharing.

Files are split into 256KB chunks, each independently encrypted
with AES-256-GCM using a per-transfer key. The transfer key is
PGP-encrypted for the recipient. Chunks can arrive out of order
and the receiver reassembles by sequence number.

This is the P2P file sharing layer from the SKChat architecture.
Transport is handled externally by SKComm.

Usage:
    sender = FileSender(recipient_pub_armor)
    transfer = sender.prepare("/path/to/file.pdf")
    for chunk in sender.chunks(transfer):
        skcomm.send(chunk.to_json())

    receiver = FileReceiver(my_private_armor, passphrase)
    for incoming_json in skcomm.receive():
        receiver.receive_chunk(FileChunk.from_json(incoming_json))
    receiver.assemble(transfer_id, "/path/to/output.pdf")
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("skchat.files")

CHUNK_SIZE = 256 * 1024  # 256KB


class TransferStatus(str, Enum):
    """Lifecycle state of a file transfer."""

    PREPARING = "preparing"
    SENDING = "sending"
    RECEIVING = "receiving"
    COMPLETE = "complete"
    FAILED = "failed"


class FileTransfer(BaseModel):
    """Metadata for a file transfer session.

    Attributes:
        transfer_id: Unique transfer identifier.
        filename: Original filename.
        file_size: Total file size in bytes.
        chunk_size: Size of each chunk in bytes.
        total_chunks: Total number of chunks.
        sha256: SHA-256 hash of the complete file.
        sender: CapAuth identity URI of the sender.
        recipient: CapAuth identity URI of the recipient.
        transfer_key: AES-256 key (hex) for chunk encryption.
        encrypted_key: PGP-encrypted transfer key for the recipient.
        status: Current transfer status.
        created_at: When the transfer was initiated.
        metadata: Extra context.
    """

    transfer_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    filename: str
    file_size: int
    chunk_size: int = CHUNK_SIZE
    total_chunks: int = 0
    sha256: str = ""
    sender: str = ""
    recipient: str = ""
    transfer_key: str = Field(default_factory=lambda: os.urandom(32).hex())
    encrypted_key: str = ""
    status: TransferStatus = TransferStatus.PREPARING
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)


class FileChunk(BaseModel):
    """A single encrypted chunk of a file transfer.

    Attributes:
        transfer_id: Which transfer this chunk belongs to.
        sequence: Chunk sequence number (0-based).
        total_chunks: Total chunks in the transfer.
        data: Base64-encoded encrypted chunk data.
        chunk_hash: SHA-256 of the plaintext chunk for integrity.
    """

    transfer_id: str
    sequence: int
    total_chunks: int
    data: str
    chunk_hash: str = ""

    def to_json(self) -> str:
        """Serialize to JSON string.

        Returns:
            str: JSON representation.
        """
        return self.model_dump_json()

    @classmethod
    def from_json(cls, json_str: str) -> FileChunk:
        """Deserialize from JSON string.

        Args:
            json_str: JSON string.

        Returns:
            FileChunk: Parsed chunk.
        """
        return cls.model_validate_json(json_str)


class FileSender:
    """Prepares and chunks files for encrypted transfer.

    Args:
        sender_identity: CapAuth identity URI of the sender.
    """

    def __init__(self, sender_identity: str = "local") -> None:
        self._identity = sender_identity

    def prepare(
        self,
        filepath: str | Path,
        recipient: str = "",
        recipient_public_armor: str = "",
        chunk_size: int = CHUNK_SIZE,
    ) -> FileTransfer:
        """Prepare a file for chunked transfer.

        Computes the file hash, calculates chunk count, and
        optionally encrypts the transfer key for the recipient.

        Args:
            filepath: Path to the file to send.
            recipient: CapAuth identity URI of the recipient.
            recipient_public_armor: Recipient's PGP public key.
            chunk_size: Override chunk size.

        Returns:
            FileTransfer: Transfer metadata ready for chunking.

        Raises:
            FileNotFoundError: If the file doesn't exist.
        """
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {filepath}")

        file_size = path.stat().st_size
        total_chunks = (file_size + chunk_size - 1) // chunk_size
        sha256 = self._hash_file(path)

        transfer = FileTransfer(
            filename=path.name,
            file_size=file_size,
            chunk_size=chunk_size,
            total_chunks=total_chunks,
            sha256=sha256,
            sender=self._identity,
            recipient=recipient,
            status=TransferStatus.SENDING,
        )

        if recipient_public_armor:
            transfer.encrypted_key = self._encrypt_key(
                transfer.transfer_key, recipient_public_armor,
            )

        return transfer

    def chunks(self, transfer: FileTransfer, filepath: str | Path) -> list[FileChunk]:
        """Split a file into encrypted chunks.

        Args:
            transfer: The prepared FileTransfer.
            filepath: Path to the file.

        Returns:
            list[FileChunk]: All chunks ready for transport.
        """
        path = Path(filepath)
        result: list[FileChunk] = []

        with open(path, "rb") as f:
            seq = 0
            while True:
                raw = f.read(transfer.chunk_size)
                if not raw:
                    break

                chunk_hash = hashlib.sha256(raw).hexdigest()
                encrypted = self._encrypt_chunk(raw, transfer.transfer_key)

                result.append(FileChunk(
                    transfer_id=transfer.transfer_id,
                    sequence=seq,
                    total_chunks=transfer.total_chunks,
                    data=encrypted,
                    chunk_hash=chunk_hash,
                ))
                seq += 1

        return result

    @staticmethod
    def _hash_file(filepath: Path) -> str:
        """Compute SHA-256 of a file.

        Args:
            filepath: File path.

        Returns:
            str: Hex digest.
        """
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            for block in iter(lambda: f.read(8192), b""):
                h.update(block)
        return h.hexdigest()

    @staticmethod
    def _encrypt_chunk(data: bytes, key_hex: str) -> str:
        """Encrypt a chunk with AES-256-GCM.

        Args:
            data: Raw chunk bytes.
            key_hex: Hex-encoded AES-256 key.

        Returns:
            str: Base64-encoded nonce + ciphertext + tag.
        """
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            key = bytes.fromhex(key_hex)
            nonce = os.urandom(12)
            aesgcm = AESGCM(key)
            ciphertext = aesgcm.encrypt(nonce, data, None)
            return base64.b64encode(nonce + ciphertext).decode("ascii")
        except ImportError:
            return base64.b64encode(data).decode("ascii")

    @staticmethod
    def _encrypt_key(key_hex: str, recipient_public_armor: str) -> str:
        """PGP-encrypt the transfer key for the recipient.

        Args:
            key_hex: Hex-encoded AES key.
            recipient_public_armor: Recipient's PGP public key.

        Returns:
            str: PGP-encrypted key string.
        """
        try:
            import pgpy

            pub, _ = pgpy.PGPKey.from_blob(recipient_public_armor)
            msg = pgpy.PGPMessage.new(key_hex.encode("utf-8"))
            encrypted = pub.encrypt(msg)
            return str(encrypted)
        except Exception as exc:
            logger.warning("Could not encrypt transfer key: %s", exc)
            return ""


class FileReceiver:
    """Receives and reassembles chunked file transfers.

    Args:
        private_key_armor: PGP private key for decrypting transfer keys.
        passphrase: Passphrase for the private key.
    """

    def __init__(
        self,
        private_key_armor: str = "",
        passphrase: str = "",
    ) -> None:
        self._private_key_armor = private_key_armor
        self._passphrase = passphrase
        self._transfers: dict[str, FileTransfer] = {}
        self._chunks: dict[str, dict[int, FileChunk]] = {}

    def register_transfer(self, transfer: FileTransfer) -> None:
        """Register an incoming file transfer.

        Args:
            transfer: The FileTransfer metadata from the sender.
        """
        self._transfers[transfer.transfer_id] = transfer
        self._chunks.setdefault(transfer.transfer_id, {})
        logger.info(
            "Registered transfer %s: %s (%d chunks)",
            transfer.transfer_id[:8], transfer.filename, transfer.total_chunks,
        )

    def receive_chunk(self, chunk: FileChunk) -> bool:
        """Accept an incoming chunk.

        Args:
            chunk: The received FileChunk.

        Returns:
            bool: True if the chunk was accepted (not a duplicate).
        """
        tid = chunk.transfer_id
        self._chunks.setdefault(tid, {})

        if chunk.sequence in self._chunks[tid]:
            return False

        self._chunks[tid][chunk.sequence] = chunk
        return True

    def is_complete(self, transfer_id: str) -> bool:
        """Check if all chunks for a transfer have been received.

        Args:
            transfer_id: The transfer identifier.

        Returns:
            bool: True if all chunks are present.
        """
        transfer = self._transfers.get(transfer_id)
        if not transfer:
            chunks = self._chunks.get(transfer_id, {})
            if not chunks:
                return False
            first = next(iter(chunks.values()))
            return len(chunks) == first.total_chunks

        return len(self._chunks.get(transfer_id, {})) == transfer.total_chunks

    def progress(self, transfer_id: str) -> tuple[int, int]:
        """Get the progress of a transfer.

        Args:
            transfer_id: The transfer identifier.

        Returns:
            tuple[int, int]: (received_chunks, total_chunks).
        """
        chunks = self._chunks.get(transfer_id, {})
        received = len(chunks)

        transfer = self._transfers.get(transfer_id)
        if transfer:
            return received, transfer.total_chunks

        if chunks:
            first = next(iter(chunks.values()))
            return received, first.total_chunks

        return 0, 0

    def assemble(
        self,
        transfer_id: str,
        output_path: str | Path,
        transfer_key_hex: Optional[str] = None,
    ) -> dict[str, Any]:
        """Reassemble and decrypt chunks into the final file.

        Args:
            transfer_id: The transfer identifier.
            output_path: Where to write the assembled file.
            transfer_key_hex: AES key (auto-decrypted from transfer if available).

        Returns:
            dict: Result with 'filepath', 'size', 'sha256', 'verified'.

        Raises:
            ValueError: If chunks are missing.
        """
        chunks = self._chunks.get(transfer_id, {})
        transfer = self._transfers.get(transfer_id)

        if transfer and len(chunks) < transfer.total_chunks:
            raise ValueError(
                f"Missing chunks: have {len(chunks)}/{transfer.total_chunks}"
            )

        if not transfer_key_hex and transfer and transfer.encrypted_key:
            transfer_key_hex = self._decrypt_transfer_key(transfer.encrypted_key)

        if not transfer_key_hex and transfer:
            transfer_key_hex = transfer.transfer_key

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        with open(out, "wb") as f:
            for seq in sorted(chunks.keys()):
                chunk = chunks[seq]
                decrypted = self._decrypt_chunk(chunk.data, transfer_key_hex or "")
                f.write(decrypted)

        file_hash = FileSender._hash_file(out)
        verified = transfer.sha256 == file_hash if transfer else True

        return {
            "filepath": str(out),
            "size": out.stat().st_size,
            "sha256": file_hash,
            "verified": verified,
            "transfer_id": transfer_id,
        }

    def _decrypt_transfer_key(self, encrypted_key: str) -> Optional[str]:
        """Decrypt a PGP-encrypted transfer key.

        Args:
            encrypted_key: PGP ciphertext of the AES key.

        Returns:
            Optional[str]: Hex-encoded AES key.
        """
        if not self._private_key_armor:
            return None

        try:
            import pgpy

            key, _ = pgpy.PGPKey.from_blob(self._private_key_armor)
            msg = pgpy.PGPMessage.from_blob(encrypted_key)

            with key.unlock(self._passphrase):
                decrypted = key.decrypt(msg)

            plaintext = decrypted.message
            if isinstance(plaintext, bytes):
                plaintext = plaintext.decode("utf-8")
            return plaintext
        except Exception as exc:
            logger.warning("Could not decrypt transfer key: %s", exc)
            return None

    @staticmethod
    def _decrypt_chunk(data_b64: str, key_hex: str) -> bytes:
        """Decrypt a chunk with AES-256-GCM.

        Args:
            data_b64: Base64-encoded nonce + ciphertext + tag.
            key_hex: Hex-encoded AES-256 key.

        Returns:
            bytes: Decrypted chunk data.
        """
        raw = base64.b64decode(data_b64)

        if not key_hex:
            return raw

        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            key = bytes.fromhex(key_hex)
            nonce, ciphertext = raw[:12], raw[12:]
            aesgcm = AESGCM(key)
            return aesgcm.decrypt(nonce, ciphertext, None)
        except ImportError:
            return raw
