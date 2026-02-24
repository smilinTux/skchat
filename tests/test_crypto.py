"""Tests for SKChat crypto — encryption, decryption, signing, verification."""

from __future__ import annotations

import pytest

from skchat.crypto import (
    ChatCrypto,
    CryptoError,
    DecryptionError,
    EncryptionError,
)
from skchat.models import ChatMessage, ContentType


PASSPHRASE = "test-passphrase-123"


class TestChatCrypto:
    """Tests for PGP encryption and signing of ChatMessages."""

    def test_init_with_valid_key(self, alice_keys: tuple[str, str]) -> None:
        """Happy path: ChatCrypto loads a valid private key."""
        private, _ = alice_keys
        crypto = ChatCrypto(private, PASSPHRASE)
        assert len(crypto.fingerprint) == 40

    def test_init_with_invalid_key(self) -> None:
        """Failure: invalid key armor should raise CryptoError."""
        with pytest.raises(CryptoError, match="Failed to load private key"):
            ChatCrypto("not-a-pgp-key", "pass")

    def test_encrypt_decrypt_roundtrip(
        self,
        alice_keys: tuple[str, str],
        bob_keys: tuple[str, str],
        sample_message: ChatMessage,
    ) -> None:
        """Happy path: message encrypted by Alice, decrypted by Bob."""
        alice_priv, alice_pub = alice_keys
        bob_priv, bob_pub = bob_keys

        alice_crypto = ChatCrypto(alice_priv, PASSPHRASE)
        bob_crypto = ChatCrypto(bob_priv, PASSPHRASE)

        encrypted = alice_crypto.encrypt_message(sample_message, bob_pub)
        assert encrypted.encrypted is True
        assert encrypted.content != sample_message.content
        assert encrypted.signature is not None

        decrypted = bob_crypto.decrypt_message(encrypted)
        assert decrypted.encrypted is False
        assert decrypted.content == sample_message.content

    def test_encrypt_already_encrypted(
        self,
        alice_keys: tuple[str, str],
        bob_keys: tuple[str, str],
        sample_message: ChatMessage,
    ) -> None:
        """Edge case: encrypting an already-encrypted message is a no-op."""
        alice_priv, _ = alice_keys
        _, bob_pub = bob_keys

        crypto = ChatCrypto(alice_priv, PASSPHRASE)
        encrypted = crypto.encrypt_message(sample_message, bob_pub)
        double_encrypted = crypto.encrypt_message(encrypted, bob_pub)

        assert double_encrypted.content == encrypted.content

    def test_decrypt_plaintext_is_noop(
        self,
        alice_keys: tuple[str, str],
        sample_message: ChatMessage,
    ) -> None:
        """Edge case: decrypting a plaintext message is a no-op."""
        alice_priv, _ = alice_keys
        crypto = ChatCrypto(alice_priv, PASSPHRASE)

        result = crypto.decrypt_message(sample_message)
        assert result.content == sample_message.content
        assert result.encrypted is False

    def test_sign_message(
        self,
        alice_keys: tuple[str, str],
        sample_message: ChatMessage,
    ) -> None:
        """Happy path: signing a message produces a signature."""
        alice_priv, _ = alice_keys
        crypto = ChatCrypto(alice_priv, PASSPHRASE)

        signed = crypto.sign_message(sample_message)
        assert signed.signature is not None
        assert len(signed.signature) > 0

    def test_verify_valid_signature(
        self,
        alice_keys: tuple[str, str],
        sample_message: ChatMessage,
    ) -> None:
        """Happy path: valid signature verifies correctly."""
        alice_priv, alice_pub = alice_keys
        crypto = ChatCrypto(alice_priv, PASSPHRASE)

        signed = crypto.sign_message(sample_message)
        assert ChatCrypto.verify_signature(signed, alice_pub) is True

    def test_verify_no_signature(
        self,
        alice_keys: tuple[str, str],
        sample_message: ChatMessage,
    ) -> None:
        """Edge case: message without signature fails verification."""
        _, alice_pub = alice_keys
        assert ChatCrypto.verify_signature(sample_message, alice_pub) is False

    def test_verify_wrong_key(
        self,
        alice_keys: tuple[str, str],
        bob_keys: tuple[str, str],
        sample_message: ChatMessage,
    ) -> None:
        """Failure: signature verified against wrong key should fail."""
        alice_priv, _ = alice_keys
        _, bob_pub = bob_keys

        crypto = ChatCrypto(alice_priv, PASSPHRASE)
        signed = crypto.sign_message(sample_message)

        assert ChatCrypto.verify_signature(signed, bob_pub) is False

    def test_fingerprint_from_armor(
        self,
        alice_keys: tuple[str, str],
    ) -> None:
        """Happy path: extract fingerprint from a public key."""
        _, alice_pub = alice_keys
        fp = ChatCrypto.fingerprint_from_armor(alice_pub)
        assert fp is not None
        assert len(fp) == 40

    def test_fingerprint_from_invalid_armor(self) -> None:
        """Failure: invalid armor returns None."""
        fp = ChatCrypto.fingerprint_from_armor("not-a-key")
        assert fp is None

    def test_encrypt_preserves_metadata(
        self,
        alice_keys: tuple[str, str],
        bob_keys: tuple[str, str],
    ) -> None:
        """Encryption preserves all message fields except content/encrypted/sig."""
        alice_priv, _ = alice_keys
        _, bob_pub = bob_keys

        msg = ChatMessage(
            sender="capauth:alice@skworld.io",
            recipient="capauth:bob@skworld.io",
            content="Metadata test",
            thread_id="thread-123",
            reply_to="msg-456",
            ttl=300,
            metadata={"custom": "value"},
        )

        crypto = ChatCrypto(alice_priv, PASSPHRASE)
        encrypted = crypto.encrypt_message(msg, bob_pub)

        assert encrypted.sender == msg.sender
        assert encrypted.recipient == msg.recipient
        assert encrypted.thread_id == msg.thread_id
        assert encrypted.reply_to == msg.reply_to
        assert encrypted.ttl == msg.ttl
        assert encrypted.metadata == msg.metadata
