"""Tests for SKChat crypto — encryption, decryption, signing, verification."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

try:
    import pgpy as _pgpy_check  # noqa: F401
    _HAS_PGPY = True
except ImportError:
    _HAS_PGPY = False

_pgpy_skip = pytest.mark.skipif(not _HAS_PGPY, reason="pgpy not installed")

from skchat.crypto import (
    ChatCrypto,
    CryptoError,
    DecryptionError,
    EncryptionError,
    verify_message,
    verify_message_signature,
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


# ---------------------------------------------------------------------------
# Module-level verify_message_signature tests
# ---------------------------------------------------------------------------


@_pgpy_skip
def test_sign_verify_roundtrip(
    alice_keys: tuple[str, str],
    sample_message: ChatMessage,
) -> None:
    """Signing a message then calling verify_message_signature returns True."""
    private_armor, _ = alice_keys
    crypto = ChatCrypto(private_armor, PASSPHRASE)
    signed = crypto.sign_message(sample_message)
    assert signed.signature is not None
    assert verify_message_signature(signed) is True


@_pgpy_skip
def test_verify_unsigned_message(sample_message: ChatMessage) -> None:
    """verify_message_signature returns False gracefully for unsigned messages."""
    assert sample_message.signature is None
    assert verify_message_signature(sample_message) is False


# ---------------------------------------------------------------------------
# Peer-store fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def peers_dir(
    tmp_path: Path,
    alice_keys: tuple[str, str],
    bob_keys: tuple[str, str],
) -> Path:
    """Temporary peers directory with alice.json and bob.json peer files.

    Each file follows the skcapstone PeerRecord schema with a ``public_key``
    field so the peer-store crypto helpers can load them without touching
    the real ~/.skcapstone/peers/ directory.

    Returns:
        Path: Isolated peers directory.
    """
    store = tmp_path / "peers"
    store.mkdir()

    _, alice_pub = alice_keys
    _, bob_pub = bob_keys

    for name, handle, pub in [
        ("Alice", "alice@skworld.io", alice_pub),
        ("Bob", "bob@skworld.io", bob_pub),
    ]:
        local = handle.split("@")[0]
        record = {
            "name": name,
            "handle": handle,
            "fingerprint": "TESTFINGERPRINT",
            "public_key": pub,
            "entity_type": "agent",
            "contact_uris": [f"capauth:{handle}"],
            "trust_level": "trusted",
        }
        (store / f"{local}.json").write_text(json.dumps(record), encoding="utf-8")

    return store


# ---------------------------------------------------------------------------
# verify_message tests
# ---------------------------------------------------------------------------


@_pgpy_skip
def test_verify_message_valid(
    alice_keys: tuple[str, str],
    sample_message: ChatMessage,
    peers_dir: Path,
) -> None:
    """verify_message resolves the sender key from the peer store and verifies."""
    alice_priv, _ = alice_keys
    crypto = ChatCrypto(alice_priv, PASSPHRASE)

    signed = crypto.sign_message(sample_message)
    # sender is "capauth:alice@skworld.io" — should resolve to alice.json
    assert verify_message(signed, peers_dir=peers_dir) is True


@_pgpy_skip
def test_verify_message_unsigned(
    sample_message: ChatMessage,
    peers_dir: Path,
) -> None:
    """verify_message returns False when the message has no signature."""
    assert verify_message(sample_message, peers_dir=peers_dir) is False


@_pgpy_skip
def test_verify_message_missing_peer(
    alice_keys: tuple[str, str],
    peers_dir: Path,
) -> None:
    """verify_message returns False when the sender has no peer file."""
    alice_priv, _ = alice_keys
    crypto = ChatCrypto(alice_priv, PASSPHRASE)

    msg = ChatMessage(
        sender="capauth:unknown@skworld.io",
        recipient="capauth:bob@skworld.io",
        content="ghost message",
    )
    signed = crypto.sign_message(msg)
    assert verify_message(signed, peers_dir=peers_dir) is False


@_pgpy_skip
def test_verify_message_wrong_signer(
    alice_keys: tuple[str, str],
    bob_keys: tuple[str, str],
    peers_dir: Path,
) -> None:
    """verify_message returns False when signed by a different key than the peer."""
    # Alice signs, but message claims to be from Bob → mismatch
    alice_priv, _ = alice_keys
    crypto = ChatCrypto(alice_priv, PASSPHRASE)

    msg = ChatMessage(
        sender="capauth:bob@skworld.io",
        recipient="capauth:alice@skworld.io",
        content="spoofed",
    )
    signed = crypto.sign_message(msg)
    assert verify_message(signed, peers_dir=peers_dir) is False


# ---------------------------------------------------------------------------
# encrypt_for_peer / decrypt_from_peer tests
# ---------------------------------------------------------------------------


@_pgpy_skip
def test_encrypt_for_peer_roundtrip(
    alice_keys: tuple[str, str],
    bob_keys: tuple[str, str],
    sample_message: ChatMessage,
    peers_dir: Path,
) -> None:
    """encrypt_for_peer + decrypt_from_peer produce the original plaintext."""
    alice_priv, _ = alice_keys
    bob_priv, _ = bob_keys

    alice_crypto = ChatCrypto(alice_priv, PASSPHRASE)
    bob_crypto = ChatCrypto(bob_priv, PASSPHRASE)

    encrypted = alice_crypto.encrypt_for_peer(sample_message, "bob", peers_dir=peers_dir)
    assert encrypted.encrypted is True

    decrypted, sig_ok = bob_crypto.decrypt_from_peer(
        encrypted, sender_handle="alice", peers_dir=peers_dir
    )
    assert decrypted.content == sample_message.content
    assert sig_ok is True


@_pgpy_skip
def test_decrypt_from_peer_no_sender(
    alice_keys: tuple[str, str],
    bob_keys: tuple[str, str],
    sample_message: ChatMessage,
    peers_dir: Path,
) -> None:
    """decrypt_from_peer with no sender_handle skips sig check and returns True."""
    alice_priv, _ = alice_keys
    bob_priv, _ = bob_keys

    alice_crypto = ChatCrypto(alice_priv, PASSPHRASE)
    bob_crypto = ChatCrypto(bob_priv, PASSPHRASE)

    encrypted = alice_crypto.encrypt_for_peer(sample_message, "bob", peers_dir=peers_dir)
    decrypted, sig_ok = bob_crypto.decrypt_from_peer(encrypted)
    assert decrypted.content == sample_message.content
    assert sig_ok is True


@_pgpy_skip
def test_encrypt_for_peer_missing_peer(
    alice_keys: tuple[str, str],
    sample_message: ChatMessage,
    peers_dir: Path,
) -> None:
    """encrypt_for_peer raises CryptoError when the peer has no file."""
    alice_priv, _ = alice_keys
    crypto = ChatCrypto(alice_priv, PASSPHRASE)

    with pytest.raises(CryptoError):
        crypto.encrypt_for_peer(sample_message, "nobody", peers_dir=peers_dir)


@_pgpy_skip
def test_encrypt_for_peer_capauth_uri(
    alice_keys: tuple[str, str],
    bob_keys: tuple[str, str],
    sample_message: ChatMessage,
    peers_dir: Path,
) -> None:
    """encrypt_for_peer accepts a full CapAuth URI as peer_handle."""
    alice_priv, _ = alice_keys
    bob_priv, _ = bob_keys

    alice_crypto = ChatCrypto(alice_priv, PASSPHRASE)
    bob_crypto = ChatCrypto(bob_priv, PASSPHRASE)

    encrypted = alice_crypto.encrypt_for_peer(
        sample_message, "capauth:bob@skworld.io", peers_dir=peers_dir
    )
    assert encrypted.encrypted is True

    decrypted, _ = bob_crypto.decrypt_from_peer(encrypted)
    assert decrypted.content == sample_message.content
