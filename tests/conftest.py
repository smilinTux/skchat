"""Shared fixtures for SKChat tests."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# skmemory path fix — must happen before any test imports skmemory.
# When pytest runs from smilintux-org/ the CWD directory 'skmemory/' shadows
# the editable install as a namespace package (PathFinder runs before the
# editable _EditableFinder in sys.meta_path).  Inserting the skmemory project
# root at position 0 lets PathFinder resolve skmemory/skmemory/__init__.py
# first, giving us the real package with MemoryStore, SQLiteBackend, etc.
# ---------------------------------------------------------------------------
import sys as _sys

_SKMEMORY_ROOT = "/home/cbrd21/dkloud.douno.it/p/smilintux-org/skmemory"
if _SKMEMORY_ROOT not in _sys.path:
    _sys.path.insert(0, _SKMEMORY_ROOT)

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

import pgpy
import pytest
from pgpy.constants import (
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


class _CapturingTransport:
    """ChatTransport stub that records every outbound message.

    Implements the same public surface as :class:`skchat.transport.ChatTransport`
    so tests can inject it as a drop-in replacement without hitting any real
    network or filesystem transport.

    Attributes:
        sent: All messages passed to :meth:`send_message` or
            :meth:`send_and_store`, in call order.
        identity: Simulated sender identity URI.
    """

    IDENTITY = "capauth:test-agent@skworld.io"

    def __init__(self) -> None:
        self.sent: list[ChatMessage] = []
        self._poll_queue: list[ChatMessage] = []
        self.identity: str = self.IDENTITY

    # ------------------------------------------------------------------
    # ChatTransport API
    # ------------------------------------------------------------------

    def send_message(
        self,
        message: ChatMessage,
        recipient_public_armor: Optional[str] = None,
    ) -> dict:
        """Capture *message* and return a successful delivery report.

        Args:
            message: The ChatMessage to capture.
            recipient_public_armor: Ignored by the stub.

        Returns:
            dict: Synthetic delivery report with ``delivered=True``.
        """
        self.sent.append(message)
        return {
            "delivered": True,
            "message_id": message.id,
            "recipient": message.recipient,
            "transport": "mock",
        }

    def send_and_store(
        self,
        recipient: str,
        content: str,
        thread_id: Optional[str] = None,
        reply_to: Optional[str] = None,
        ttl: Optional[int] = None,
        recipient_public_armor: Optional[str] = None,
    ) -> dict:
        """Compose, capture, and return a delivery report.

        Args:
            recipient: CapAuth identity URI of the recipient.
            content: Message text to capture.
            thread_id: Optional thread identifier.
            reply_to: Optional message ID being replied to.
            ttl: Ignored by the stub.
            recipient_public_armor: Ignored by the stub.

        Returns:
            dict: Synthetic delivery report with ``delivered=True``.
        """
        msg = ChatMessage(
            sender=self.IDENTITY,
            recipient=recipient,
            content=content,
            content_type=ContentType.PLAIN,
            thread_id=thread_id,
            reply_to_id=reply_to,
        )
        self.sent.append(msg)
        return {
            "delivered": True,
            "message_id": msg.id,
            "recipient": recipient,
            "transport": "mock",
        }

    def poll_inbox(
        self,
        sender_public_armor: Optional[str] = None,
    ) -> list[ChatMessage]:
        """Drain and return any messages queued via :meth:`inject`.

        Args:
            sender_public_armor: Ignored by the stub.

        Returns:
            list[ChatMessage]: Messages previously injected, then cleared.
        """
        msgs = list(self._poll_queue)
        self._poll_queue.clear()
        return msgs

    def inject(self, message: ChatMessage) -> None:
        """Queue *message* so it is returned by the next :meth:`poll_inbox`.

        Args:
            message: A ChatMessage to stage as an incoming message.
        """
        self._poll_queue.append(message)

    def send_typing_indicator(
        self,
        recipient: str,
        thread_id: Optional[str] = None,
    ) -> None:
        """No-op typing indicator for the stub.

        Args:
            recipient: Ignored.
            thread_id: Ignored.
        """


@pytest.fixture()
def mock_transport() -> _CapturingTransport:
    """Capturing ChatTransport stub that records all outbound messages.

    Returns a :class:`_CapturingTransport` instance.  Inspect
    ``mock_transport.sent`` in assertions to verify what was sent.

    Returns:
        _CapturingTransport: Ready-to-use capturing transport.
    """
    return _CapturingTransport()


# ---------------------------------------------------------------------------
# AgentMessenger fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_messenger(tmp_skchat_dir: Path):
    """AgentMessenger backed by an isolated FileTransport and temp history.

    History is stored under tmp_skchat_dir/history so tests are fully
    isolated from the real ~/.skchat data.

    Args:
        tmp_skchat_dir: Temp skchat home (provides history dir + env vars).

    Returns:
        AgentMessenger: Ready for agent-to-agent messaging in tests.
    """
    from skcomm.transports.file import FileTransport

    from skchat.agent_comm import AgentMessenger
    from skchat.history import ChatHistory
    from skchat.transport import ChatTransport

    inbox = tmp_skchat_dir / "inbox"
    outbox = tmp_skchat_dir / "outbox"
    inbox.mkdir(exist_ok=True)
    outbox.mkdir(exist_ok=True)

    file_transport = FileTransport(inbox_path=inbox, outbox_path=outbox)
    history = ChatHistory(history_dir=tmp_skchat_dir / "history")

    chat_transport = ChatTransport(
        skcomm=file_transport,
        history=history,
        identity="capauth:test-agent@skworld.io",
    )

    return AgentMessenger(
        identity="capauth:test-agent@skworld.io",
        history=history,
        transport=chat_transport,
    )


# ---------------------------------------------------------------------------
# SQLite / ChatHistory fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    """Temporary directory suitable as an SQLiteBackend ``base_path``.

    Creates a ``skchat-db/`` sub-directory under *tmp_path* so each
    test gets its own isolated SQLite store.

    Args:
        tmp_path: Pytest-provided unique temporary directory.

    Returns:
        Path: Isolated directory for the SQLiteBackend.
    """
    db_dir = tmp_path / "skchat-db"
    db_dir.mkdir()
    return db_dir


@pytest.fixture()
def chat_history(tmp_path: Path, tmp_db: Path):
    """ChatHistory backed by an isolated JSONL dir and SQLite store.

    JSONL files go to ``tmp_path/history/``; the optional SKMemory
    MemoryStore uses a SQLiteBackend rooted at *tmp_db*.  Falls back to
    a store-less ChatHistory if skmemory's SQLiteBackend is unavailable.

    Args:
        tmp_path: Pytest-provided unique temporary directory.
        tmp_db: Temp dir for the SQLiteBackend (from :func:`tmp_db`).

    Returns:
        ChatHistory: Fully isolated history instance.
    """
    from skchat.history import ChatHistory

    history_dir = tmp_path / "history"
    history_dir.mkdir()

    store = None
    try:
        from skmemory import MemoryStore
        from skmemory.backends.sqlite_backend import SQLiteBackend

        store = MemoryStore(primary=SQLiteBackend(base_path=str(tmp_db)))
    except Exception:  # pragma: no cover — optional dep
        pass

    return ChatHistory(store=store, history_dir=history_dir)


# ---------------------------------------------------------------------------
# Mock MemoryStore fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_memory() -> MagicMock:
    """MagicMock configured to behave like a skmemory MemoryStore.

    Returns a mock whose key methods (``snapshot``, ``list_memories``,
    ``search``) have sensible defaults so tests can inject it as a
    ChatHistory store without needing a real SQLite file.

    The mock's ``snapshot`` returns a new MagicMock each call with a
    unique ``id`` attribute so ID-tracking code does not collide.

    Returns:
        MagicMock: Drop-in MemoryStore substitute.
    """
    store = MagicMock(name="MemoryStore")
    _call_count = [0]

    def _snapshot(*_args: Any, **_kwargs: Any) -> MagicMock:
        _call_count[0] += 1
        mem = MagicMock(name=f"Memory#{_call_count[0]}")
        mem.id = f"mock-memory-{_call_count[0]:04d}"
        mem.tags = _kwargs.get("tags", [])
        mem.metadata = _kwargs.get("metadata", {})
        mem.content = _kwargs.get("content", "")
        mem.title = _kwargs.get("title", "")
        mem.created_at = datetime.now(timezone.utc)
        return mem

    store.snapshot.side_effect = _snapshot
    store.list_memories.return_value = []
    store.search.return_value = []
    return store


# ---------------------------------------------------------------------------
# PeerDiscovery fixture
# ---------------------------------------------------------------------------

_TEST_PEERS = [
    {
        "name": "Alice",
        "handle": "alice@skworld.io",
        "fingerprint": "AAAA111122223333",
        "entity_type": "agent",
        "contact_uris": ["capauth:alice@skworld.io"],
        "trust_level": "trusted",
        "capabilities": ["chat", "files"],
        "email": "alice@skworld.io",
        "added_at": "2025-01-01T00:00:00Z",
        "last_seen": None,
        "source": "test",
        "notes": "Test peer Alice",
    },
    {
        "name": "Bob",
        "handle": "bob@skworld.io",
        "fingerprint": "BBBB444455556666",
        "entity_type": "agent",
        "contact_uris": ["capauth:bob@skworld.io"],
        "trust_level": "trusted",
        "capabilities": ["chat"],
        "email": "bob@skworld.io",
        "added_at": "2025-01-01T00:00:00Z",
        "last_seen": None,
        "source": "test",
        "notes": "Test peer Bob",
    },
    {
        "name": "Charlie",
        "handle": "charlie@skworld.io",
        "fingerprint": "CCCC777788889999",
        "entity_type": "human",
        "contact_uris": ["capauth:charlie@skworld.io"],
        "trust_level": "observer",
        "capabilities": [],
        "email": "charlie@skworld.io",
        "added_at": "2025-01-02T00:00:00Z",
        "last_seen": None,
        "source": "test",
        "notes": "Test peer Charlie",
    },
]


@pytest.fixture()
def peer_discovery_fixture(tmp_path: Path):
    """PeerDiscovery backed by a temp peers directory with known test peers.

    Writes three JSON peer files (alice, bob, charlie) under
    ``tmp_path/peers/`` so tests have predictable, real-filesystem
    lookup without touching ``~/.skcapstone/peers/``.

    Use ``peer_discovery_fixture.list_peers()`` or
    ``peer_discovery_fixture.get_peer("alice")`` in assertions.

    Args:
        tmp_path: Pytest-provided unique temporary directory.

    Returns:
        PeerDiscovery: Instance pointed at the isolated peers directory.
    """
    from skchat.peer_discovery import PeerDiscovery

    peers_dir = tmp_path / "peers"
    peers_dir.mkdir()

    for peer in _TEST_PEERS:
        local = peer["handle"].split("@")[0]
        (peers_dir / f"{local}.json").write_text(
            json.dumps(peer, indent=2),
            encoding="utf-8",
        )

    return PeerDiscovery(peers_dir=peers_dir)


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


# ---------------------------------------------------------------------------
# transport — top-level alias for the capturing transport stub
# ---------------------------------------------------------------------------


@pytest.fixture()
def transport() -> _CapturingTransport:
    """Capturing ChatTransport stub (top-level alias for mock_transport).

    Prefer this fixture in tests that only need to verify outbound traffic
    and do not care about SKComm internals.  Tests that patch SKComm
    directly should use their own local ``transport`` fixture instead.

    Returns:
        _CapturingTransport: Fresh stub; inspect ``.sent`` in assertions.
    """
    return _CapturingTransport()


# ---------------------------------------------------------------------------
# peer_alice — pre-built Alice peer dict
# ---------------------------------------------------------------------------

_PEER_ALICE: dict[str, Any] = {
    "name": "Alice",
    "handle": "alice@skworld.io",
    "fingerprint": "AAAA111122223333",
    "entity_type": "agent",
    "contact_uris": ["capauth:alice@skworld.io"],
    "trust_level": "trusted",
    "capabilities": ["chat", "files"],
    "email": "alice@skworld.io",
    "added_at": "2025-01-01T00:00:00Z",
    "last_seen": None,
    "source": "test",
    "notes": "Test peer Alice",
}


@pytest.fixture()
def peer_alice() -> dict[str, Any]:
    """Pre-built peer dict representing Alice (capauth:alice@skworld.io).

    Identical to the Alice entry written by :func:`peer_discovery_fixture`
    so tests can reference peer metadata without constructing a full
    ``PeerDiscovery`` instance.

    Returns:
        dict: Alice's peer record as a plain Python dict.
    """
    return dict(_PEER_ALICE)


# ---------------------------------------------------------------------------
# event_loop — per-test asyncio event loop
# ---------------------------------------------------------------------------


@pytest.fixture()
def event_loop():
    """Provide a fresh asyncio event loop for each test function.

    Yields the loop and closes it after the test completes, ensuring no
    state leaks between async tests.

    Yields:
        asyncio.AbstractEventLoop: A new, running-ready event loop.
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
