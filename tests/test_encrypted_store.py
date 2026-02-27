"""Tests for encrypted message storage."""

import os
import pytest
from unittest.mock import MagicMock, patch

from skchat.models import ChatMessage, ContentType, Thread
from skchat.encrypted_store import (
    ContentEncryptor,
    EncryptedChatHistory,
    StorageKeyDeriver,
)


@pytest.fixture
def storage_key() -> bytes:
    """A deterministic 32-byte test key."""
    return StorageKeyDeriver.derive_key(
        "AABBCCDD" * 5,
        salt=b"test-salt-for-skchat-storage-key",
    )


@pytest.fixture
def mock_history():
    """A mock ChatHistory."""
    history = MagicMock()
    history.store_message.return_value = "mem-001"
    history.store_thread.return_value = "thread-001"
    history.get_thread_messages.return_value = []
    history.get_conversation.return_value = []
    history.search_messages.return_value = []
    history.list_threads.return_value = []
    history.message_count.return_value = 0
    history.get_thread.return_value = None
    return history


@pytest.fixture
def encrypted_history(mock_history, storage_key):
    """An EncryptedChatHistory with mock backing store."""
    return EncryptedChatHistory(history=mock_history, storage_key=storage_key)


def _msg(content="Hello world", sender="capauth:alice@test", recipient="capauth:bob@test"):
    return ChatMessage(sender=sender, recipient=recipient, content=content)


class TestStorageKeyDeriver:
    def test_derive_key_deterministic(self):
        salt = b"fixed-salt-for-testing"
        key1 = StorageKeyDeriver.derive_key("AABB" * 10, salt=salt)
        key2 = StorageKeyDeriver.derive_key("AABB" * 10, salt=salt)
        assert key1 == key2
        assert len(key1) == 32

    def test_different_fingerprint_different_key(self):
        salt = b"fixed-salt-for-testing"
        key1 = StorageKeyDeriver.derive_key("AAAA" * 10, salt=salt)
        key2 = StorageKeyDeriver.derive_key("BBBB" * 10, salt=salt)
        assert key1 != key2

    def test_different_salt_different_key(self):
        key1 = StorageKeyDeriver.derive_key("AABB" * 10, salt=b"salt-one")
        key2 = StorageKeyDeriver.derive_key("AABB" * 10, salt=b"salt-two")
        assert key1 != key2


class TestContentEncryptor:
    def test_encrypt_decrypt_roundtrip(self, storage_key):
        plaintext = "Hello, sovereign world!"
        encrypted = ContentEncryptor.encrypt(plaintext, storage_key)
        assert encrypted != plaintext

        decrypted = ContentEncryptor.decrypt(encrypted, storage_key)
        assert decrypted == plaintext

    def test_different_nonces(self, storage_key):
        plaintext = "Same message"
        e1 = ContentEncryptor.encrypt(plaintext, storage_key)
        e2 = ContentEncryptor.encrypt(plaintext, storage_key)
        assert e1 != e2

    def test_wrong_key_fails(self, storage_key):
        plaintext = "Secret"
        encrypted = ContentEncryptor.encrypt(plaintext, storage_key)

        wrong_key = os.urandom(32)
        with pytest.raises(ValueError, match="Decryption failed"):
            ContentEncryptor.decrypt(encrypted, wrong_key)

    def test_unicode_content(self, storage_key):
        plaintext = "Sovereignty is key! staycuriousANDkeepsmilin"
        encrypted = ContentEncryptor.encrypt(plaintext, storage_key)
        decrypted = ContentEncryptor.decrypt(encrypted, storage_key)
        assert decrypted == plaintext

    def test_empty_string(self, storage_key):
        encrypted = ContentEncryptor.encrypt("", storage_key)
        decrypted = ContentEncryptor.decrypt(encrypted, storage_key)
        assert decrypted == ""

    def test_large_content(self, storage_key):
        plaintext = "Sovereign data! " * 10000
        encrypted = ContentEncryptor.encrypt(plaintext, storage_key)
        decrypted = ContentEncryptor.decrypt(encrypted, storage_key)
        assert decrypted == plaintext


class TestEncryptedChatHistory:
    def test_store_message_encrypts_content(self, encrypted_history, mock_history):
        msg = _msg("Secret message")
        encrypted_history.store_message(msg)

        mock_history.store_message.assert_called_once()
        stored_msg = mock_history.store_message.call_args[0][0]
        assert stored_msg.content.startswith("enc:aes256gcm:")
        assert "Secret message" not in stored_msg.content
        assert stored_msg.metadata["encrypted_at_rest"] is True

    def test_store_thread_not_encrypted(self, encrypted_history, mock_history):
        thread = Thread(
            id="t1",
            title="Test Thread",
            participants=["capauth:alice@test"],
        )
        encrypted_history.store_thread(thread)
        mock_history.store_thread.assert_called_once_with(thread)

    def test_get_thread_messages_decrypts(self, encrypted_history, mock_history, storage_key):
        # Simulate an encrypted message in storage
        plaintext = "Decrypted content"
        encrypted_content = ContentEncryptor.encrypt(plaintext, storage_key)
        marked = f"enc:aes256gcm:{encrypted_content}"

        mock_history.get_thread_messages.return_value = [
            {
                "content": marked,
                "sender": "capauth:alice@test",
                "encrypted_at_rest": True,
            }
        ]

        messages = encrypted_history.get_thread_messages("t1")
        assert len(messages) == 1
        assert messages[0]["content"] == plaintext

    def test_get_thread_messages_plaintext_passthrough(self, encrypted_history, mock_history):
        mock_history.get_thread_messages.return_value = [
            {
                "content": "Plain content (not encrypted)",
                "sender": "capauth:alice@test",
            }
        ]

        messages = encrypted_history.get_thread_messages("t1")
        assert messages[0]["content"] == "Plain content (not encrypted)"

    def test_get_conversation_decrypts(self, encrypted_history, mock_history, storage_key):
        plaintext = "Secret conversation"
        encrypted_content = ContentEncryptor.encrypt(plaintext, storage_key)
        marked = f"enc:aes256gcm:{encrypted_content}"

        mock_history.get_conversation.return_value = [
            {"content": marked, "sender": "capauth:alice@test"}
        ]

        messages = encrypted_history.get_conversation(
            "capauth:alice@test", "capauth:bob@test"
        )
        assert messages[0]["content"] == plaintext

    def test_decryption_failure_graceful(self, encrypted_history, mock_history):
        mock_history.get_thread_messages.return_value = [
            {
                "content": "enc:aes256gcm:invalid-base64-data!!!",
                "sender": "capauth:alice@test",
            }
        ]

        messages = encrypted_history.get_thread_messages("t1")
        assert messages[0]["content"] == "[decryption failed]"
        assert messages[0]["decryption_error"] is True

    def test_list_threads_passthrough(self, encrypted_history, mock_history):
        mock_history.list_threads.return_value = [
            {"thread_id": "t1", "title": "Test"}
        ]
        threads = encrypted_history.list_threads()
        assert threads == [{"thread_id": "t1", "title": "Test"}]

    def test_message_count_passthrough(self, encrypted_history, mock_history):
        mock_history.message_count.return_value = 42
        assert encrypted_history.message_count() == 42

    def test_get_thread_passthrough(self, encrypted_history, mock_history):
        mock_history.get_thread.return_value = {"thread_id": "t1"}
        assert encrypted_history.get_thread("t1") == {"thread_id": "t1"}


class TestEncryptDecryptIntegration:
    """End-to-end: store encrypted, retrieve decrypted."""

    def test_roundtrip_through_encrypted_history(self, storage_key):
        """Simulates full store -> retrieve -> decrypt cycle."""
        encryptor = ContentEncryptor()
        marker = EncryptedChatHistory.ENCRYPTED_MARKER

        # Encrypt
        plaintext = "Top secret sovereign data"
        encrypted = encryptor.encrypt(plaintext, storage_key)
        stored_content = f"{marker}{encrypted}"

        # Verify encrypted
        assert plaintext not in stored_content
        assert stored_content.startswith(marker)

        # Decrypt
        encrypted_b64 = stored_content[len(marker):]
        decrypted = encryptor.decrypt(encrypted_b64, storage_key)
        assert decrypted == plaintext
