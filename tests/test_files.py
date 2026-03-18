"""Tests for SKChat encrypted file transfer."""

from __future__ import annotations

import os
from pathlib import Path

import pgpy
import pytest
from pgpy.constants import (
    HashAlgorithm,
    KeyFlags,
    PubKeyAlgorithm,
    SymmetricKeyAlgorithm,
)

from skchat.files import (
    CHUNK_SIZE,
    FileChunk,
    FileReceiver,
    FileSender,
    FileTransferService,
    TransferStatus,
)

PASSPHRASE = "file-test-2026"


def _keygen() -> tuple[str, str]:
    """Generate a test PGP keypair."""
    key = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    uid = pgpy.PGPUID.new("FileTest", email="file@test.io")
    key.add_uid(
        uid,
        usage={KeyFlags.Sign, KeyFlags.Certify},
        hashes=[HashAlgorithm.SHA256],
        ciphers=[SymmetricKeyAlgorithm.AES256],
    )
    sub = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    key.add_subkey(sub, usage={KeyFlags.EncryptCommunications, KeyFlags.EncryptStorage})
    key.protect(PASSPHRASE, SymmetricKeyAlgorithm.AES256, HashAlgorithm.SHA256)
    return str(key), str(key.pubkey)


@pytest.fixture(scope="session")
def receiver_keys() -> tuple[str, str]:
    """Receiver keypair for PGP key encryption tests."""
    return _keygen()


def _create_test_file(tmp_path: Path, name: str, size: int) -> Path:
    """Create a test file with random data."""
    path = tmp_path / name
    path.write_bytes(os.urandom(size))
    return path


class TestFileSenderPrepare:
    """Tests for file preparation."""

    def test_prepare_small_file(self, tmp_path: Path) -> None:
        """Happy path: prepare a small file."""
        f = _create_test_file(tmp_path, "small.txt", 100)
        sender = FileSender("capauth:alice@test")
        transfer = sender.prepare(f, recipient="capauth:bob@test")

        assert transfer.filename == "small.txt"
        assert transfer.file_size == 100
        assert transfer.total_chunks == 1
        assert len(transfer.sha256) == 64
        assert transfer.status == TransferStatus.SENDING

    def test_prepare_multi_chunk_file(self, tmp_path: Path) -> None:
        """File larger than chunk size gets multiple chunks."""
        size = CHUNK_SIZE * 3 + 100
        f = _create_test_file(tmp_path, "big.bin", size)
        sender = FileSender()
        transfer = sender.prepare(f, chunk_size=CHUNK_SIZE)

        assert transfer.total_chunks == 4

    def test_prepare_missing_file_raises(self) -> None:
        """Preparing a non-existent file raises FileNotFoundError."""
        sender = FileSender()
        with pytest.raises(FileNotFoundError):
            sender.prepare("/nonexistent/file.txt")

    def test_prepare_encrypts_key(self, tmp_path: Path, receiver_keys: tuple[str, str]) -> None:
        """Transfer key is PGP-encrypted when public key is provided."""
        _, pub = receiver_keys
        f = _create_test_file(tmp_path, "secret.dat", 50)
        sender = FileSender()
        transfer = sender.prepare(f, recipient_public_armor=pub)

        assert transfer.encrypted_key != ""
        assert "PGP MESSAGE" in transfer.encrypted_key


class TestChunking:
    """Tests for file chunking and encryption."""

    def test_chunk_small_file(self, tmp_path: Path) -> None:
        """Small file produces one chunk."""
        f = _create_test_file(tmp_path, "tiny.bin", 50)
        sender = FileSender()
        transfer = sender.prepare(f)
        chunks = sender.chunks(transfer, f)

        assert len(chunks) == 1
        assert chunks[0].sequence == 0
        assert chunks[0].transfer_id == transfer.transfer_id
        assert chunks[0].chunk_hash != ""

    def test_chunk_multi_chunk_file(self, tmp_path: Path) -> None:
        """Multi-chunk file splits correctly."""
        size = CHUNK_SIZE * 2 + 100
        f = _create_test_file(tmp_path, "multi.bin", size)
        sender = FileSender()
        transfer = sender.prepare(f)
        chunks = sender.chunks(transfer, f)

        assert len(chunks) == 3
        assert [c.sequence for c in chunks] == [0, 1, 2]
        assert all(c.total_chunks == 3 for c in chunks)

    def test_chunks_are_encrypted(self, tmp_path: Path) -> None:
        """Chunk data is different from raw file content."""
        f = _create_test_file(tmp_path, "enc.bin", 100)
        raw = f.read_bytes()
        sender = FileSender()
        transfer = sender.prepare(f)
        chunks = sender.chunks(transfer, f)

        import base64

        decoded = base64.b64decode(chunks[0].data)
        assert decoded != raw


class TestFileReceiver:
    """Tests for receiving and assembling files."""

    def test_full_transfer_roundtrip(self, tmp_path: Path) -> None:
        """Happy path: send chunks then reassemble into identical file."""
        original = _create_test_file(tmp_path, "original.bin", 1000)
        original_data = original.read_bytes()

        sender = FileSender("capauth:alice@test")
        transfer = sender.prepare(original, recipient="capauth:bob@test")
        chunks = sender.chunks(transfer, original)

        receiver = FileReceiver()
        receiver.register_transfer(transfer)
        for chunk in chunks:
            accepted = receiver.receive_chunk(chunk)
            assert accepted is True

        assert receiver.is_complete(transfer.transfer_id)

        output = tmp_path / "received.bin"
        result = receiver.assemble(
            transfer.transfer_id,
            output,
            transfer_key_hex=transfer.transfer_key,
        )

        assert result["verified"] is True
        assert result["size"] == 1000
        assert output.read_bytes() == original_data

    def test_large_file_roundtrip(self, tmp_path: Path) -> None:
        """Multi-chunk file survives full send/receive cycle."""
        size = CHUNK_SIZE * 3 + 500
        original = _create_test_file(tmp_path, "large.bin", size)

        sender = FileSender()
        transfer = sender.prepare(original)
        chunks = sender.chunks(transfer, original)

        receiver = FileReceiver()
        receiver.register_transfer(transfer)
        for chunk in chunks:
            receiver.receive_chunk(chunk)

        output = tmp_path / "large_received.bin"
        result = receiver.assemble(
            transfer.transfer_id,
            output,
            transfer_key_hex=transfer.transfer_key,
        )

        assert result["verified"] is True
        assert result["size"] == size

    def test_duplicate_chunk_rejected(self, tmp_path: Path) -> None:
        """Duplicate chunk is not accepted twice."""
        f = _create_test_file(tmp_path, "dup.bin", 50)
        sender = FileSender()
        transfer = sender.prepare(f)
        chunks = sender.chunks(transfer, f)

        receiver = FileReceiver()
        assert receiver.receive_chunk(chunks[0]) is True
        assert receiver.receive_chunk(chunks[0]) is False

    def test_progress_tracking(self, tmp_path: Path) -> None:
        """Progress reports received vs total chunks."""
        size = CHUNK_SIZE * 3
        f = _create_test_file(tmp_path, "progress.bin", size)
        sender = FileSender()
        transfer = sender.prepare(f)
        chunks = sender.chunks(transfer, f)

        receiver = FileReceiver()
        receiver.register_transfer(transfer)

        received, total = receiver.progress(transfer.transfer_id)
        assert received == 0
        assert total == 3

        receiver.receive_chunk(chunks[0])
        received, total = receiver.progress(transfer.transfer_id)
        assert received == 1

    def test_incomplete_raises(self, tmp_path: Path) -> None:
        """Assembling with missing chunks raises ValueError."""
        f = _create_test_file(tmp_path, "incomplete.bin", CHUNK_SIZE * 2)
        sender = FileSender()
        transfer = sender.prepare(f)
        chunks = sender.chunks(transfer, f)

        receiver = FileReceiver()
        receiver.register_transfer(transfer)
        receiver.receive_chunk(chunks[0])

        with pytest.raises(ValueError, match="Missing chunks"):
            receiver.assemble(
                transfer.transfer_id, tmp_path / "out.bin", transfer_key_hex=transfer.transfer_key
            )

    def test_pgp_key_encrypted_transfer(
        self,
        tmp_path: Path,
        receiver_keys: tuple[str, str],
    ) -> None:
        """Full transfer with PGP-encrypted transfer key."""
        priv, pub = receiver_keys
        original = _create_test_file(tmp_path, "pgp.bin", 500)

        sender = FileSender()
        transfer = sender.prepare(original, recipient_public_armor=pub)
        chunks = sender.chunks(transfer, original)

        receiver = FileReceiver(private_key_armor=priv, passphrase=PASSPHRASE)
        receiver.register_transfer(transfer)
        for chunk in chunks:
            receiver.receive_chunk(chunk)

        output = tmp_path / "pgp_received.bin"
        result = receiver.assemble(transfer.transfer_id, output)

        assert result["verified"] is True
        assert output.read_bytes() == original.read_bytes()


class TestFileChunkSerialization:
    """Tests for chunk JSON serialization."""

    def test_roundtrip(self) -> None:
        """Chunk survives JSON roundtrip."""
        chunk = FileChunk(
            transfer_id="t-001",
            sequence=0,
            total_chunks=3,
            data="base64data",
            chunk_hash="abc123",
        )
        json_str = chunk.to_json()
        loaded = FileChunk.from_json(json_str)
        assert loaded.transfer_id == "t-001"
        assert loaded.sequence == 0
        assert loaded.data == "base64data"


class TestFileTransferService:
    """Tests for the high-level FileTransferService."""

    def test_send_file_returns_transfer_id(self, tmp_path: Path) -> None:
        """send_file() returns a non-empty UUID string."""
        f = _create_test_file(tmp_path, "doc.txt", 1000)
        service = FileTransferService(identity="capauth:alice@test", base_dir=tmp_path / ".skchat")
        transfer_id = service.send_file("capauth:bob@test", f)

        assert transfer_id != ""
        assert len(transfer_id) == 36  # UUID format

    def test_send_file_persists_metadata(self, tmp_path: Path) -> None:
        """Metadata JSON is written to the transfers directory."""
        import json

        f = _create_test_file(tmp_path, "meta_test.bin", 500)
        base = tmp_path / ".skchat"
        service = FileTransferService(identity="capauth:alice@test", base_dir=base)
        transfer_id = service.send_file("capauth:bob@test", f)

        meta_path = base / "transfers" / f"{transfer_id}.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["filename"] == "meta_test.bin"
        assert meta["recipient"] == "capauth:bob@test"
        assert meta["sha256"] != ""
        assert meta["direction"] == "outbound"
        assert "total_chunks" in meta

    def test_send_file_missing_raises(self, tmp_path: Path) -> None:
        """send_file() raises FileNotFoundError for missing files."""
        service = FileTransferService(identity="capauth:alice@test", base_dir=tmp_path / ".skchat")
        with pytest.raises(FileNotFoundError):
            service.send_file("capauth:bob@test", tmp_path / "nonexistent.bin")

    def test_list_transfers_empty(self, tmp_path: Path) -> None:
        """list_transfers() returns [] when no transfers exist."""
        service = FileTransferService(identity="capauth:alice@test", base_dir=tmp_path / ".skchat")
        assert service.list_transfers() == []

    def test_list_transfers_shows_sent(self, tmp_path: Path) -> None:
        """list_transfers() includes outbound transfers."""
        f = _create_test_file(tmp_path, "share.bin", 100)
        base = tmp_path / ".skchat"
        service = FileTransferService(identity="capauth:alice@test", base_dir=base)
        transfer_id = service.send_file("capauth:bob@test", f)

        transfers = service.list_transfers()
        assert len(transfers) == 1
        assert transfers[0]["transfer_id"] == transfer_id
        assert transfers[0]["direction"] == "outbound"
        assert "progress" in transfers[0]

    def test_progress_unknown_returns_zero(self, tmp_path: Path) -> None:
        """progress() returns 0.0 for an unknown transfer_id."""
        service = FileTransferService(identity="capauth:alice@test", base_dir=tmp_path / ".skchat")
        assert service.progress("nonexistent-id") == 0.0

    def test_progress_after_send_no_transport(self, tmp_path: Path) -> None:
        """progress() is between 0.0 and 1.0 after send with no transport."""
        f = _create_test_file(tmp_path, "prog.bin", 100)
        base = tmp_path / ".skchat"
        service = FileTransferService(identity="capauth:alice@test", base_dir=base)
        transfer_id = service.send_file("capauth:bob@test", f)

        p = service.progress(transfer_id)
        assert 0.0 <= p <= 1.0

    def test_store_incoming_chunk_and_receive_file(self, tmp_path: Path) -> None:
        """Full inbound roundtrip via store_incoming_chunk + receive_file."""
        original = _create_test_file(tmp_path, "incoming.bin", 1000)
        sender = FileSender("capauth:bob@test")
        transfer = sender.prepare(original, recipient="capauth:alice@test")
        chunks = sender.chunks(transfer, original)

        base = tmp_path / ".skchat"
        service = FileTransferService(identity="capauth:alice@test", base_dir=base)

        service.store_incoming_chunk(
            {
                "type": "FILE_TRANSFER_INIT",
                "transfer_id": transfer.transfer_id,
                "filename": transfer.filename,
                "size": transfer.file_size,
                "sha256": transfer.sha256,
                "total_chunks": transfer.total_chunks,
                "sender": transfer.sender,
                "transfer_key": transfer.transfer_key,
            }
        )

        for chunk in chunks:
            service.store_incoming_chunk(
                {
                    "type": "FILE_CHUNK",
                    "transfer_id": transfer.transfer_id,
                    "chunk_idx": chunk.sequence,
                    "total_chunks": chunk.total_chunks,
                    "data_b64": chunk.data,
                    "chunk_hash": chunk.chunk_hash,
                }
            )

        service.store_incoming_chunk(
            {
                "type": "FILE_TRANSFER_DONE",
                "transfer_id": transfer.transfer_id,
            }
        )

        out_path = service.receive_file(transfer.transfer_id)
        assert out_path is not None
        assert out_path.exists()
        assert out_path.read_bytes() == original.read_bytes()

    def test_receive_file_incomplete_returns_none(self, tmp_path: Path) -> None:
        """receive_file() returns None when chunks are missing."""
        original = _create_test_file(tmp_path, "partial.bin", CHUNK_SIZE * 2)
        sender = FileSender("capauth:bob@test")
        transfer = sender.prepare(original, recipient="capauth:alice@test")
        chunks = sender.chunks(transfer, original)

        base = tmp_path / ".skchat"
        service = FileTransferService(identity="capauth:alice@test", base_dir=base)

        service.store_incoming_chunk(
            {
                "type": "FILE_TRANSFER_INIT",
                "transfer_id": transfer.transfer_id,
                "filename": transfer.filename,
                "size": transfer.file_size,
                "sha256": transfer.sha256,
                "total_chunks": transfer.total_chunks,
                "sender": transfer.sender,
                "transfer_key": transfer.transfer_key,
            }
        )
        # Only first chunk — deliberately incomplete
        service.store_incoming_chunk(
            {
                "type": "FILE_CHUNK",
                "transfer_id": transfer.transfer_id,
                "chunk_idx": chunks[0].sequence,
                "total_chunks": chunks[0].total_chunks,
                "data_b64": chunks[0].data,
                "chunk_hash": chunks[0].chunk_hash,
            }
        )

        result = service.receive_file(transfer.transfer_id)
        assert result is None

    def test_receive_file_no_metadata_returns_none(self, tmp_path: Path) -> None:
        """receive_file() returns None for an unknown transfer_id."""
        service = FileTransferService(identity="capauth:alice@test", base_dir=tmp_path / ".skchat")
        result = service.receive_file("totally-unknown-id")
        assert result is None

    def test_receive_file_custom_output_dir(self, tmp_path: Path) -> None:
        """receive_file(output_dir=...) saves to the requested directory."""
        original = _create_test_file(tmp_path, "custom_out.bin", 200)
        sender = FileSender("capauth:bob@test")
        transfer = sender.prepare(original, recipient="capauth:alice@test")
        chunks = sender.chunks(transfer, original)

        base = tmp_path / ".skchat"
        service = FileTransferService(identity="capauth:alice@test", base_dir=base)

        service.store_incoming_chunk(
            {
                "type": "FILE_TRANSFER_INIT",
                "transfer_id": transfer.transfer_id,
                "filename": transfer.filename,
                "size": transfer.file_size,
                "sha256": transfer.sha256,
                "total_chunks": transfer.total_chunks,
                "sender": transfer.sender,
                "transfer_key": transfer.transfer_key,
            }
        )
        for chunk in chunks:
            service.store_incoming_chunk(
                {
                    "type": "FILE_CHUNK",
                    "transfer_id": transfer.transfer_id,
                    "chunk_idx": chunk.sequence,
                    "total_chunks": chunk.total_chunks,
                    "data_b64": chunk.data,
                    "chunk_hash": chunk.chunk_hash,
                }
            )

        custom_dir = tmp_path / "my_downloads"
        out_path = service.receive_file(transfer.transfer_id, output_dir=custom_dir)
        assert out_path is not None
        assert custom_dir in out_path.parents
        assert out_path.read_bytes() == original.read_bytes()

    def test_send_with_mock_transport(self, tmp_path: Path) -> None:
        """send_file() calls skcomm.send() for INIT, each CHUNK, and DONE."""
        import json

        class _MockSKComm:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def send(self, recipient: str, message: str, **kwargs: object) -> None:
                self.calls.append({"recipient": recipient, "message": json.loads(message)})

        f = _create_test_file(tmp_path, "transport.bin", 1000)
        mock = _MockSKComm()
        service = FileTransferService(
            identity="capauth:alice@test",
            skcomm=mock,
            base_dir=tmp_path / ".skchat",
        )
        transfer_id = service.send_file("capauth:bob@test", f)

        types = [c["message"]["type"] for c in mock.calls]
        assert types[0] == "FILE_TRANSFER_INIT"
        assert all(t == "FILE_CHUNK" for t in types[1:-1])
        assert types[-1] == "FILE_TRANSFER_DONE"
        # Every message has the right transfer_id
        assert all(c["message"]["transfer_id"] == transfer_id for c in mock.calls)
