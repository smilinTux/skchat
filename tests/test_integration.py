"""Integration tests: full send/receive roundtrip over file:// transport.

All tests use temporary directories under /tmp/skchat-test-*/ to simulate
a shared-filesystem transport (no network, no daemon, no real SKComm server).

Tests
-----
test_send_to_self     — loopback: agent sends to itself and receives back
test_group_send       — group message composed → sent → received by peer
test_history_persist  — JSONL history save/load roundtrip (file durability)
test_peer_discovery   — peer files loaded, looked up, identity resolved

None of these tests require skmemory or a running SKComm daemon.

Run (from ~/):
    cd ~ && python -m pytest \\
        /home/cbrd21/dkloud.douno.it/p/smilintux-org/skchat/tests/test_integration.py \\
        -v -m 'not e2e_live'
"""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock

import pytest

from skchat.models import ChatMessage, ContentType, DeliveryStatus
from skchat.transport import ChatTransport

# ---------------------------------------------------------------------------
# Minimal file-based SKComm stub
# ---------------------------------------------------------------------------


class _FileSKComm:
    """Minimal file-based SKComm stub for offline integration testing.

    Writes outbound messages as JSON files to *outbox_dir*.
    Reads (and removes) inbound messages from *inbox_dir*.

    For loopback (sender == receiver) use the same path for both.

    This mirrors the ``_FileSKComm`` used in ``test_e2e_live.py`` so that
    ``ChatTransport.send_message()`` / ``poll_inbox()`` work without any
    real SKComm dependency.

    Args:
        outbox_dir: Directory where sent envelope files are written.
        inbox_dir: Directory where inbox envelope files are polled.
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
        """Write the serialised message payload to a unique file in outbox_dir."""
        filename = f"{uuid.uuid4()}.json"
        (self._outbox / filename).write_text(message, encoding="utf-8")
        return SimpleNamespace(delivered=True, successful_transport="file")

    def receive(self) -> list:
        """Read and consume all ``*.json`` files from inbox_dir.

        Each file yields one ``SimpleNamespace(payload=SimpleNamespace(content=…))``
        that ``ChatTransport._extract_payload()`` can unwrap.
        """
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
# Minimal in-memory ChatHistory stub (no skmemory required)
# ---------------------------------------------------------------------------


class _InMemoryHistory:
    """Minimal ChatHistory stub that stores ChatMessage objects in a list.

    Implements only the surface required by ChatTransport so no skmemory
    or SQLite dependency is introduced in the transport-level tests.
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
# Pytest fixture: isolated /tmp/skchat-test-*/ directory
# ---------------------------------------------------------------------------


@pytest.fixture()
def test_dir() -> Path:
    """Create and yield an isolated ``/tmp/skchat-test-*/`` directory.

    The directory (and everything under it) is removed after the test
    completes, regardless of whether the test passes or fails.

    Yields:
        Path: A fresh temporary directory under ``/tmp/skchat-test-*/``.
    """
    d = Path(tempfile.mkdtemp(prefix="skchat-test-", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test 1 — loopback send/receive
# ---------------------------------------------------------------------------


def test_send_to_self(test_dir: Path) -> None:
    """Agent sends a message to itself and receives it back via loopback.

    Setup:
        A single ChatTransport with ``outbox_dir == inbox_dir`` (loopback).
        The envelope written during ``send_and_store()`` is immediately
        available for ``poll_inbox()`` in the same process.

    Assertions:
        - ``send_and_store`` reports ``delivered=True`` and a non-empty
          ``message_id``.
        - ``poll_inbox`` returns exactly 1 message.
        - The received message has the original content, sender == recipient,
          and delivery_status == DELIVERED.
        - A second ``poll_inbox`` call returns nothing (file consumed on first read).
    """
    transport_dir = test_dir / "transport"
    transport_dir.mkdir()

    identity = "capauth:self@skworld.io"
    history = _InMemoryHistory()
    skcomm = _FileSKComm(outbox_dir=transport_dir, inbox_dir=transport_dir)
    transport = ChatTransport(skcomm=skcomm, history=history, identity=identity)

    content = "Loopback echo — the sovereign agent hears itself."
    result = transport.send_and_store(recipient=identity, content=content)

    assert result["delivered"] is True, f"send_and_store failed: {result}"
    assert result.get("message_id"), "message_id missing from result"
    sent_id = result["message_id"]

    # --- receive ---
    received = transport.poll_inbox()

    assert len(received) == 1, f"expected 1 message, got {len(received)}"
    msg = received[0]
    assert msg.content == content
    assert msg.sender == identity
    assert msg.recipient == identity
    assert msg.id == sent_id
    assert msg.delivery_status == DeliveryStatus.DELIVERED

    # File must be consumed — second poll returns nothing
    second_poll = transport.poll_inbox()
    assert second_poll == [], f"expected empty second poll, got {second_poll}"

    # History: 2 entries (sent copy + received copy)
    assert history.message_count() >= 2


# ---------------------------------------------------------------------------
# Test 2 — group send
# ---------------------------------------------------------------------------


def test_group_send(test_dir: Path) -> None:
    """Group message composed by creator is received by a member via shared dir.

    Setup:
        A ``GroupChat`` with three members (opus/creator, lumina, chef).
        All transports share the same directory (simulating a synced folder).
        Opus composes a group message, sends it via ``send_message()``;
        lumina polls the same directory and receives it.

    Assertions:
        - ``compose_group_message`` produces a ChatMessage with
          ``thread_id == group.id`` and ``recipient == "group:{group.id}"``.
        - ``send_message`` reports ``delivered=True``.
        - Lumina receives exactly 1 message via ``poll_inbox``.
        - Received message: correct content, sender == opus, thread_id == group.id.
        - Received message delivery_status is DELIVERED.
    """
    from skchat.group import GroupChat, MemberRole, ParticipantType

    shared_dir = test_dir / "shared"
    shared_dir.mkdir()

    OPUS = "capauth:opus@skworld.io"
    LUMINA = "capauth:lumina@skworld.io"
    CHEF = "capauth:chef@skworld.io"

    # Create group and add members
    group = GroupChat.create(
        name="Integration Test Group",
        creator_uri=OPUS,
        description="File-transport integration test",
    )
    group.add_member(
        identity_uri=LUMINA,
        participant_type=ParticipantType.AGENT,
        role=MemberRole.MEMBER,
    )
    group.add_member(
        identity_uri=CHEF,
        participant_type=ParticipantType.AGENT,
        role=MemberRole.MEMBER,
    )

    assert group.member_count == 3, f"expected creator + 2 members, got {group.member_count}"

    # Compose the group message
    group_content = "Hello integration test group — all agents copy."
    msg = group.compose_group_message(sender_uri=OPUS, content=group_content)

    assert msg is not None, "compose_group_message returned None"
    assert msg.thread_id == group.id, f"thread_id mismatch: {msg.thread_id!r} != {group.id!r}"
    assert msg.recipient == f"group:{group.id}", f"recipient mismatch: {msg.recipient!r}"
    assert msg.sender == OPUS

    # Opus sends via transport to the shared dir
    opus_history = _InMemoryHistory()
    opus_skcomm = _FileSKComm(outbox_dir=shared_dir, inbox_dir=shared_dir)
    opus_transport = ChatTransport(skcomm=opus_skcomm, history=opus_history, identity=OPUS)

    result = opus_transport.send_message(msg)
    assert result["delivered"] is True, f"group send_message failed: {result}"

    # Lumina polls the shared inbox — must receive the group message
    lumina_history = _InMemoryHistory()
    lumina_skcomm = _FileSKComm(outbox_dir=shared_dir, inbox_dir=shared_dir)
    lumina_transport = ChatTransport(skcomm=lumina_skcomm, history=lumina_history, identity=LUMINA)

    received = lumina_transport.poll_inbox()

    assert len(received) == 1, f"lumina expected 1 message, got {len(received)}"
    rx = received[0]
    assert rx.content == group_content
    assert rx.sender == OPUS
    assert rx.thread_id == group.id
    assert rx.recipient == f"group:{group.id}"
    assert rx.delivery_status == DeliveryStatus.DELIVERED


# ---------------------------------------------------------------------------
# Test 3 — JSONL history persistence
# ---------------------------------------------------------------------------


def test_history_persist(test_dir: Path) -> None:
    """ChatMessage objects saved to JSONL files survive and reload correctly.

    Uses ``ChatHistory.save()`` (JSONL append) and ``ChatHistory.load()``
    (JSONL read) directly.  No skmemory MemoryStore is exercised so the
    test is self-contained.

    Setup:
        A ``ChatHistory`` backed only by a temp JSONL directory.
        Three ChatMessages (alice→bob, alice→bob, bob→alice) are saved.

    Assertions:
        - ``load()`` returns all 3 messages.
        - All original content strings are present in loaded messages.
        - ``load(peer="capauth:alice@skworld.io")`` returns only alice's
          messages (as sender or recipient).
        - ``load(limit=2)`` returns at most 2 messages.
        - Timestamps survive round-trip (are non-None datetime objects).
    """
    from skchat.history import ChatHistory

    history_dir = test_dir / "history"
    history_dir.mkdir()

    # Use a MagicMock as the store so _make_default_store() is not called
    # and no real ~/.skchat/memory directory is touched during the test.
    history = ChatHistory(store=MagicMock(), history_dir=history_dir)

    ALICE = "capauth:alice@skworld.io"
    BOB = "capauth:bob@skworld.io"

    messages = [
        ChatMessage(
            sender=ALICE,
            recipient=BOB,
            content="First message from alice to bob.",
            content_type=ContentType.PLAIN,
        ),
        ChatMessage(
            sender=ALICE,
            recipient=BOB,
            content="Second message from alice to bob.",
            content_type=ContentType.PLAIN,
        ),
        ChatMessage(
            sender=BOB,
            recipient=ALICE,
            content="Reply from bob to alice.",
            content_type=ContentType.PLAIN,
        ),
    ]

    # Save all three to JSONL
    for m in messages:
        history.save(m)

    # Verify the JSONL file was created
    jsonl_files = list(history_dir.glob("*.jsonl"))
    assert len(jsonl_files) == 1, f"expected 1 JSONL file, got {jsonl_files}"

    # load() should return all 3 messages
    loaded = history.load(limit=50)
    assert len(loaded) == 3, f"expected 3 messages, got {len(loaded)}: {loaded}"

    loaded_contents = {m.content for m in loaded}
    for original in messages:
        assert original.content in loaded_contents, (
            f"'{original.content}' not found in loaded messages"
        )

    # Timestamps are preserved as datetime objects
    for m in loaded:
        assert m.timestamp is not None, "timestamp should not be None"

    # Filter by peer: alice appears as sender or recipient in all 3 messages
    alice_msgs = history.load(peer=ALICE, limit=50)
    assert len(alice_msgs) == 3, f"expected 3 messages involving alice, got {len(alice_msgs)}"

    # Filter by peer: bob appears in all 3 as well (sender or recipient)
    bob_msgs = history.load(peer=BOB, limit=50)
    assert len(bob_msgs) == 3

    # limit parameter is respected
    limited = history.load(limit=2)
    assert len(limited) == 2


# ---------------------------------------------------------------------------
# Test 4 — peer discovery
# ---------------------------------------------------------------------------


def test_peer_discovery(test_dir: Path) -> None:
    """Peers written as JSON files are loadable, searchable, and resolvable.

    Setup:
        Three peer JSON files (alice, bob, charlie) under a temp peers dir.

    Assertions:
        - ``list_peers()`` returns all 3 peers.
        - ``get_peer("alice")`` finds Alice by short handle.
        - ``get_peer("alice@skworld.io")`` also resolves Alice.
        - Alice's ``contact_uris`` includes ``"capauth:alice@skworld.io"``.
        - ``resolve_identity("alice")`` returns ``"capauth:alice@skworld.io"``.
        - ``to_tab_completions()`` includes handles for all 3 peers.
        - ``get_peer("nobody")`` returns None (unknown peer).
    """
    from skchat.peer_discovery import PeerDiscovery

    peers_dir = test_dir / "peers"
    peers_dir.mkdir()

    peers_data = [
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

    for peer in peers_data:
        local = peer["handle"].split("@")[0]
        (peers_dir / f"{local}.json").write_text(json.dumps(peer, indent=2), encoding="utf-8")

    disc = PeerDiscovery(peers_dir=peers_dir)

    # list_peers returns all 3
    all_peers = disc.list_peers()
    assert len(all_peers) == 3, f"expected 3 peers, got {len(all_peers)}: {all_peers}"
    names = {p["name"] for p in all_peers}
    assert names == {"Alice", "Bob", "Charlie"}

    # get_peer by short handle
    alice = disc.get_peer("alice")
    assert alice is not None, "get_peer('alice') returned None"
    assert alice["name"] == "Alice"
    assert "capauth:alice@skworld.io" in alice["contact_uris"]

    # get_peer by full handle
    alice_full = disc.get_peer("alice@skworld.io")
    assert alice_full is not None, "get_peer('alice@skworld.io') returned None"
    assert alice_full["name"] == "Alice"

    # resolve_identity maps short name to full CapAuth URI
    uri = disc.resolve_identity("alice")
    assert uri == "capauth:alice@skworld.io", f"unexpected URI: {uri!r}"

    # to_tab_completions includes local parts of all handles
    completions = disc.to_tab_completions()
    for expected_handle in ("alice", "bob", "charlie"):
        assert expected_handle in completions, (
            f"'{expected_handle}' missing from tab completions: {completions}"
        )

    # Unknown peer resolves to None
    nobody = disc.get_peer("nobody")
    assert nobody is None, f"expected None for unknown peer, got {nobody!r}"
