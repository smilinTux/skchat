"""Shared fixtures for SKChat tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pgpy
import pytest
from pgpy.constants import (
    EllipticCurveOID,
    HashAlgorithm,
    KeyFlags,
    PubKeyAlgorithm,
    SymmetricKeyAlgorithm,
)

from skchat.models import ChatMessage, ContentType, Thread


PASSPHRASE = "test-passphrase-123"


def _generate_test_keypair(name: str, email: str) -> tuple[str, str]:
    """Generate a PGP keypair for testing.

    Args:
        name: Display name for the UID.
        email: Email for the UID.

    Returns:
        tuple[str, str]: (private_armor, public_armor).
    """
    key = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    uid = pgpy.PGPUID.new(name, email=email)
    key.add_uid(
        uid,
        usage={KeyFlags.Sign, KeyFlags.Certify},
        hashes=[HashAlgorithm.SHA256],
        ciphers=[SymmetricKeyAlgorithm.AES256],
    )

    enc_subkey = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    key.add_subkey(
        enc_subkey,
        usage={KeyFlags.EncryptCommunications, KeyFlags.EncryptStorage},
    )

    key.protect(PASSPHRASE, SymmetricKeyAlgorithm.AES256, HashAlgorithm.SHA256)
    return str(key), str(key.pubkey)


@pytest.fixture(scope="session")
def alice_keys() -> tuple[str, str]:
    """Generate Alice's PGP keypair (session-scoped for speed).

    Returns:
        tuple[str, str]: (private_armor, public_armor).
    """
    return _generate_test_keypair("Alice", "alice@skworld.io")


@pytest.fixture(scope="session")
def bob_keys() -> tuple[str, str]:
    """Generate Bob's PGP keypair (session-scoped for speed).

    Returns:
        tuple[str, str]: (private_armor, public_armor).
    """
    return _generate_test_keypair("Bob", "bob@skworld.io")


@pytest.fixture()
def sample_message() -> ChatMessage:
    """A basic ChatMessage for testing.

    Returns:
        ChatMessage: Message from Alice to Bob.
    """
    return ChatMessage(
        sender="capauth:alice@skworld.io",
        recipient="capauth:bob@skworld.io",
        content="Hello from the sovereign side!",
        content_type=ContentType.PLAIN,
    )


@pytest.fixture()
def sample_thread() -> Thread:
    """A basic Thread for testing.

    Returns:
        Thread: Thread with Alice and Bob.
    """
    return Thread(
        title="Project Discussion",
        participants=["capauth:alice@skworld.io", "capauth:bob@skworld.io"],
    )
