"""E2E live tests for SKChat using file-based transport (no network).

Full ChatTransport.send() -> shared-dir -> ChatTransport.poll_inbox()
roundtrip with no daemon, no real SKComm, and no network.

A shared temporary directory acts as the transport medium: one side
writes an envelope file, the other reads it back.  This mirrors the
Syncthing file-share scenario used in production.

The tests are self-contained and always runnable:
- No skmemory required (in-memory history stub used)
- No SKComm dependency (file-based stub)
- No daemon required

Run with:
    pytest tests/test_e2e_live.py -v
    pytest tests/test_e2e_live.py -v -m e2e_live
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from skchat.models import ChatMessage, ContentType, DeliveryStatus
from skchat.transport import ChatTransport

# ---------------------------------------------------------------------------
# pytest mark
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.e2e_live


# ---------------------------------------------------------------------------
# File-based SKComm stub
# ---------------------------------------------------------------------------


class _FileSKComm:
    """Minimal file-based SKComm stub for offline E2E testing.

    Writes outbound envelopes as JSON files to ``outbox_dir``.
    Reads (and consumes) inbound envelopes from ``inbox_dir``.

    For loopback tests (sender == receiver) use the same path for both.

    Args:
        outbox_dir: Directory where sent messages are written.
        inbox_dir: Directory where received messages are read from.
    """

    def __init__(self, outbox_dir: Path, inbox_dir: Path) -> None:
        self._outbox = outbox_dir
        self._inbox = inbox_dir

    def send(
        self,
        recipient: str,
        message: str,
        thread_id: Optional[str] = None,
        in_reply_to: Optional[str] = None,
    ) -> SimpleNamespace:
        """Write message payload to a file in outbox_dir."""
        filename = f"{uuid.uuid4()}.json"
        (self._outbox / filename).write_text(message, encoding="utf-8")
        return SimpleNamespace(delivered=True, successful_transport="file")

    def receive(self) -> list:
        """Read and consume all message files from inbox_dir."""
        envelopes = []
        for f in sorted(self._inbox.glob("*.json")):
            try:
                content = f.read_text(encoding="utf-8")
                f.unlink()
                envelopes.append(SimpleNamespace(payload=SimpleNamespace(content=content)))
            except Exception:
                continue
        return envelopes


# ---------------------------------------------------------------------------
# In-memory ChatHistory stub
# ---------------------------------------------------------------------------


class _InMemoryHistory:
    """Minimal in-memory ChatHistory stub for isolation.

    Stores ChatMessage objects in a plain list so tests run without
    skmemory.  Not thread-safe (not required for single-threaded tests).
    """

    def __init__(self) -> None:
        self._messages: list[ChatMessage] = []

    def store_message(self, message: ChatMessage) -> str:
        self._messages.append(message)
        return message.id

    def all_messages(self) -> list[ChatMessage]:
        return list(self._messages)

    def message_count(self) -> int:
        return len(self._messages)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def transport_dir(tmp_path: Path) -> Path:
    """Shared transport directory (simulates Syncthing share).

    Returns:
        Path: Temp dir used as both outbox and inbox (loopback).
    """
    d = tmp_path / "transport"
    d.mkdir()
    return d


@pytest.fixture()
def history() -> _InMemoryHistory:
    """Fresh in-memory history for each test."""
    return _InMemoryHistory()


@pytest.fixture()
def sender_transport(transport_dir: Path, history: _InMemoryHistory) -> ChatTransport:
    """ChatTransport for the sender (opus)."""
    skcomm = _FileSKComm(outbox_dir=transport_dir, inbox_dir=transport_dir)
    return ChatTransport(
        skcomm=skcomm,
        history=history,
        identity="capauth:opus@skworld.io",
    )


@pytest.fixture()
def receiver_transport(transport_dir: Path, history: _InMemoryHistory) -> ChatTransport:
    """ChatTransport for the receiver (lumina) — same shared dir."""
    skcomm = _FileSKComm(outbox_dir=transport_dir, inbox_dir=transport_dir)
    return ChatTransport(
        skcomm=skcomm,
        history=history,
        identity="capauth:lumina@skworld.io",
    )


# ---------------------------------------------------------------------------
# Tests — Basic roundtrip
# ---------------------------------------------------------------------------


class TestFiletransportRoundtrip:
    """Full send -> file-share -> poll_inbox roundtrip."""

    def test_send_and_receive_single_message(
        self,
        sender_transport: ChatTransport,
        receiver_transport: ChatTransport,
    ) -> None:
        """Message sent by sender is received by receiver via shared dir."""
        content = "HelloLumina! E2E file-transport test."

        result = sender_transport.send_and_store(
            recipient="capauth:lumina@skworld.io",
            content=content,
        )

        assert result["delivered"] is True, f"send failed: {result}"

        received = receiver_transport.poll_inbox()

        assert len(received) == 1, f"expected 1 message, got {len(received)}"
        assert received[0].content == content

    def test_message_content_is_preserved(
        self,
        sender_transport: ChatTransport,
        receiver_transport: ChatTransport,
    ) -> None:
        """Exact content bytes are preserved through file transport."""
        content = "Exact content: unicode ✓ emoji 🛰 newline\nand tab\tpreserved."

        sender_transport.send_and_store(
            recipient="capauth:lumina@skworld.io",
            content=content,
        )
        received = receiver_transport.poll_inbox()

        assert len(received) == 1
        assert received[0].content == content

    def test_sender_identity_is_preserved(
        self,
        sender_transport: ChatTransport,
        receiver_transport: ChatTransport,
    ) -> None:
        """Sender identity URI survives serialisation through transport."""
        sender_transport.send_and_store(
            recipient="capauth:lumina@skworld.io",
            content="Identity check.",
        )
        received = receiver_transport.poll_inbox()

        assert len(received) == 1
        assert received[0].sender == "capauth:opus@skworld.io"

    def test_recipient_identity_is_preserved(
        self,
        sender_transport: ChatTransport,
        receiver_transport: ChatTransport,
    ) -> None:
        """Recipient identity URI survives serialisation."""
        sender_transport.send_and_store(
            recipient="capauth:lumina@skworld.io",
            content="Recipient check.",
        )
        received = receiver_transport.poll_inbox()

        assert received[0].recipient == "capauth:lumina@skworld.io"

    def test_timestamp_is_recent(
        self,
        sender_transport: ChatTransport,
        receiver_transport: ChatTransport,
    ) -> None:
        """Message timestamp is close to wall-clock at send time."""
        before = datetime.now(timezone.utc)

        sender_transport.send_and_store(
            recipient="capauth:lumina@skworld.io",
            content="Timestamp validation.",
        )
        received = receiver_transport.poll_inbox()

        after = datetime.now(timezone.utc)

        assert len(received) == 1
        ts = received[0].timestamp
        # Normalise to aware
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        assert before <= ts <= after, (
            f"timestamp {ts!r} is outside send window [{before!r}, {after!r}]"
        )

    def test_delivery_status_set_to_delivered_on_receive(
        self,
        sender_transport: ChatTransport,
        receiver_transport: ChatTransport,
    ) -> None:
        """poll_inbox marks received messages as DELIVERED."""
        sender_transport.send_and_store(
            recipient="capauth:lumina@skworld.io",
            content="Status check.",
        )
        received = receiver_transport.poll_inbox()

        assert received[0].delivery_status == DeliveryStatus.DELIVERED

    def test_message_id_is_uuid(
        self,
        sender_transport: ChatTransport,
        receiver_transport: ChatTransport,
    ) -> None:
        """Message ID is a non-empty UUID string, preserved across transport."""
        result = sender_transport.send_and_store(
            recipient="capauth:lumina@skworld.io",
            content="ID check.",
        )
        received = receiver_transport.poll_inbox()

        msg_id = result["message_id"]
        assert msg_id
        assert received[0].id == msg_id


# ---------------------------------------------------------------------------
# Tests — Thread / reply metadata
# ---------------------------------------------------------------------------


class TestMetadataPropagation:
    """Verify thread_id, reply_to, and ttl survive the transport."""

    def test_thread_id_is_propagated(
        self,
        sender_transport: ChatTransport,
        receiver_transport: ChatTransport,
    ) -> None:
        """thread_id set on send is present on received message."""
        thread_id = "e2e-test-thread-001"

        sender_transport.send_and_store(
            recipient="capauth:lumina@skworld.io",
            content="Threaded message.",
            thread_id=thread_id,
        )
        received = receiver_transport.poll_inbox()

        assert len(received) == 1
        assert received[0].thread_id == thread_id

    def test_reply_to_is_propagated(
        self,
        sender_transport: ChatTransport,
        receiver_transport: ChatTransport,
    ) -> None:
        """reply_to message ID is preserved through file transport."""
        parent_id = str(uuid.uuid4())

        sender_transport.send_and_store(
            recipient="capauth:lumina@skworld.io",
            content="Reply message.",
            reply_to=parent_id,
        )
        received = receiver_transport.poll_inbox()

        assert received[0].reply_to_id == parent_id

    def test_ttl_is_propagated(
        self,
        sender_transport: ChatTransport,
        receiver_transport: ChatTransport,
    ) -> None:
        """TTL (time-to-live) value survives serialisation."""
        sender_transport.send_and_store(
            recipient="capauth:lumina@skworld.io",
            content="Ephemeral message.",
            ttl=60,
        )
        received = receiver_transport.poll_inbox()

        assert received[0].ttl == 60
        assert received[0].is_ephemeral() is True

    def test_content_type_markdown_default(
        self,
        sender_transport: ChatTransport,
        receiver_transport: ChatTransport,
    ) -> None:
        """Default content_type is MARKDOWN."""
        sender_transport.send_and_store(
            recipient="capauth:lumina@skworld.io",
            content="Markdown check.",
        )
        received = receiver_transport.poll_inbox()

        assert received[0].content_type == ContentType.MARKDOWN


# ---------------------------------------------------------------------------
# Tests — Multiple messages
# ---------------------------------------------------------------------------


class TestMultipleMessages:
    """Batch send / receive correctness."""

    def test_multiple_messages_all_received(
        self,
        sender_transport: ChatTransport,
        receiver_transport: ChatTransport,
    ) -> None:
        """All messages sent in a batch are received without loss."""
        payloads = [f"Batch message {i}" for i in range(5)]

        for content in payloads:
            sender_transport.send_and_store(
                recipient="capauth:lumina@skworld.io",
                content=content,
            )

        received = receiver_transport.poll_inbox()

        assert len(received) == 5
        received_contents = {m.content for m in received}
        for content in payloads:
            assert content in received_contents, f"'{content}' not found in received messages"

    def test_poll_inbox_is_idempotent_after_consume(
        self,
        sender_transport: ChatTransport,
        receiver_transport: ChatTransport,
    ) -> None:
        """Second poll_inbox returns nothing (files consumed on first read)."""
        sender_transport.send_and_store(
            recipient="capauth:lumina@skworld.io",
            content="Once only.",
        )

        first = receiver_transport.poll_inbox()
        second = receiver_transport.poll_inbox()

        assert len(first) == 1
        assert len(second) == 0

    def test_messages_stored_in_history(
        self,
        sender_transport: ChatTransport,
        receiver_transport: ChatTransport,
        history: _InMemoryHistory,
    ) -> None:
        """All sent and received messages end up in ChatHistory."""
        sender_transport.send_and_store(
            recipient="capauth:lumina@skworld.io",
            content="History tracking test.",
        )
        receiver_transport.poll_inbox()

        # 2 entries: one for the sent message, one for the received copy
        assert history.message_count() >= 2


# ---------------------------------------------------------------------------
# Tests — Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Graceful degradation under adverse conditions."""

    def test_bad_envelope_is_silently_skipped(
        self,
        transport_dir: Path,
        history: _InMemoryHistory,
    ) -> None:
        """Malformed JSON in transport dir is skipped without crashing."""
        # Inject a corrupt file directly into the transport dir
        (transport_dir / "corrupt.json").write_text("{{not valid json", encoding="utf-8")

        skcomm = _FileSKComm(outbox_dir=transport_dir, inbox_dir=transport_dir)
        transport = ChatTransport(
            skcomm=skcomm,
            history=history,
            identity="capauth:lumina@skworld.io",
        )

        # Should not raise, should return empty list
        received = transport.poll_inbox()
        assert received == []

    def test_empty_transport_dir_returns_empty(
        self,
        receiver_transport: ChatTransport,
    ) -> None:
        """poll_inbox on empty dir returns empty list."""
        received = receiver_transport.poll_inbox()
        assert received == []

    def test_send_result_contains_message_id(
        self,
        sender_transport: ChatTransport,
    ) -> None:
        """send_and_store result includes a non-empty message_id."""
        result = sender_transport.send_and_store(
            recipient="capauth:lumina@skworld.io",
            content="Result shape check.",
        )

        assert "message_id" in result
        assert result["message_id"]
        assert isinstance(result["message_id"], str)

    def test_send_result_contains_transport_name(
        self,
        sender_transport: ChatTransport,
    ) -> None:
        """send_and_store reports the successful transport name."""
        result = sender_transport.send_and_store(
            recipient="capauth:lumina@skworld.io",
            content="Transport name check.",
        )

        assert result.get("transport") == "file"


# ---------------------------------------------------------------------------
# Tests — Direct ChatMessage construction
# ---------------------------------------------------------------------------


class TestDirectChatMessageRoundtrip:
    """Send pre-built ChatMessage objects through file transport."""

    def test_send_explicit_chatmessage(
        self,
        sender_transport: ChatTransport,
        receiver_transport: ChatTransport,
    ) -> None:
        """send_message() accepts a pre-built ChatMessage."""
        msg = ChatMessage(
            sender="capauth:opus@skworld.io",
            recipient="capauth:lumina@skworld.io",
            content="Explicit ChatMessage via send_message().",
            content_type=ContentType.PLAIN,
        )

        result = sender_transport.send_message(msg)
        received = receiver_transport.poll_inbox()

        assert result["delivered"] is True
        assert len(received) == 1
        assert received[0].id == msg.id
        assert received[0].content == msg.content
        assert received[0].content_type == ContentType.PLAIN

    def test_send_message_with_metadata(
        self,
        sender_transport: ChatTransport,
        receiver_transport: ChatTransport,
    ) -> None:
        """Custom metadata dict is preserved across transport."""
        msg = ChatMessage(
            sender="capauth:opus@skworld.io",
            recipient="capauth:lumina@skworld.io",
            content="Message with metadata.",
            metadata={"custom_key": "custom_value", "priority": 3},
        )

        sender_transport.send_message(msg)
        received = receiver_transport.poll_inbox()

        assert received[0].metadata.get("custom_key") == "custom_value"
        assert received[0].metadata.get("priority") == 3
