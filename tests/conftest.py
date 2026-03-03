"""Shared fixtures for SKChat tests."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

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


# ---------------------------------------------------------------------------
# Directory + environment fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_skchat_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Temporary ~/.skchat directory with standard sub-dirs and env vars set.

    Creates history/, groups/, and presence/ under *tmp_path* and points
    SKCHAT_IDENTITY + SKCHAT_HOME at the temp location so no test ever
    touches the real user home.

    Args:
        tmp_path: Pytest-provided unique temporary directory.
        monkeypatch: Pytest monkeypatch fixture for env-var isolation.

    Returns:
        Path: Root of the temporary skchat home (tmp_path).
    """
    for subdir in ("history", "groups", "presence"):
        (tmp_path / subdir).mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("SKCHAT_IDENTITY", "capauth:test-agent@skworld.io")
    monkeypatch.setenv("SKCHAT_HOME", str(tmp_path))

    return tmp_path


# ---------------------------------------------------------------------------
# Transport fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_transport(tmp_path: Path):
    """FileTransport wired to isolated inbox/outbox directories.

    Uses skcomm's FileTransport so tests exercise real serialisation
    without hitting the filesystem outside *tmp_path*.

    Args:
        tmp_path: Pytest-provided unique temporary directory.

    Returns:
        FileTransport: Ready to send/receive envelopes in tmp dirs.
    """
    from skcomm.transports.file import FileTransport

    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    inbox.mkdir()
    outbox.mkdir()

    return FileTransport(inbox_path=inbox, outbox_path=outbox)


# ---------------------------------------------------------------------------
# AgentMessenger fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_messenger(tmp_skchat_dir: Path, mock_transport):
    """AgentMessenger backed by a FileTransport in a temp directory.

    History is stored under tmp_skchat_dir/history so tests are fully
    isolated from the real ~/.skchat data.

    Args:
        tmp_skchat_dir: Temp skchat home (provides history dir + env vars).
        mock_transport: Isolated FileTransport fixture.

    Returns:
        AgentMessenger: Ready for agent-to-agent messaging in tests.
    """
    from skchat.agent_comm import AgentMessenger
    from skchat.history import ChatHistory
    from skchat.transport import ChatTransport

    history = ChatHistory(history_dir=tmp_skchat_dir / "history")

    # Wrap FileTransport in ChatTransport so AgentMessenger gets the right API.
    chat_transport = ChatTransport(
        skcomm=mock_transport,
        history=history,
        identity="capauth:test-agent@skworld.io",
    )

    return AgentMessenger(
        identity="capauth:test-agent@skworld.io",
        history=history,
        transport=chat_transport,
    )


# ---------------------------------------------------------------------------
# LLM bridge stub
# ---------------------------------------------------------------------------


class _CannedLLMBridge:
    """Minimal LLMBridge stand-in that returns a configurable canned response.

    Call ``set_response(text)`` in a test to change what ``generate()``
    returns.  By default it returns an empty string so tests that do not
    care about the LLM output stay noise-free.
    """

    def __init__(self) -> None:
        self._response: str = ""

    def set_response(self, text: str) -> None:
        """Configure the canned response returned by generate().

        Args:
            text: Text to return on the next generate() call.
        """
        self._response = text

    def generate(
        self,
        prompt: str,
        system: str = "",
        **kwargs: Any,
    ) -> str:
        """Return the pre-configured canned response.

        Args:
            prompt: Ignored in the stub.
            system: Ignored in the stub.
            **kwargs: Ignored.

        Returns:
            str: The canned response set via set_response().
        """
        return self._response


@pytest.fixture()
def mock_llm_bridge() -> _CannedLLMBridge:
    """Stub LLMBridge with a configurable canned response.

    Usage in a test::

        def test_something(mock_llm_bridge):
            mock_llm_bridge.set_response("Mocked LLM output")
            result = mock_llm_bridge.generate("some prompt")
            assert result == "Mocked LLM output"

    Returns:
        _CannedLLMBridge: Stub with generate() + set_response().
    """
    return _CannedLLMBridge()
