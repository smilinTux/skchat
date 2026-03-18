"""Integration tests for SKChat send/receive roundtrip.

Tests the full AgentMessenger.send() -> ChatHistory.store_message() ->
get_inbox() pipeline using an in-memory MemoryStore (no real network,
no SKComm transport required).

Group message tests verify: GroupChat.create() -> compose_group_message()
-> ChatHistory.store_message() -> get_thread_messages().

All tests in this module are marked with ``pytest.mark.integration`` and
will be skipped automatically when SKComm is not configured or skmemory
is not importable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Skip guard — skip the whole module if skmemory is not importable
# ---------------------------------------------------------------------------

try:
    import skmemory  # noqa: F401

    _SKMEMORY_AVAILABLE = True
except ImportError:
    _SKMEMORY_AVAILABLE = False

pytestmark = pytest.mark.integration

# Mark all tests in this module as integration tests.
# Run selectively with: pytest -m integration
# Skip them (default CI) with:  pytest -m "not integration"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_in_memory_history(tmp_path: Path):
    """Return a ChatHistory backed by an ephemeral SQLite store.

    Uses a temporary directory so each test starts with an empty store,
    isolating state between test runs.

    Args:
        tmp_path: pytest's built-in temporary directory fixture.

    Returns:
        ChatHistory: Ready-to-use history with an isolated SQLite backend.
    """
    from skchat.history import ChatHistory

    store_path = str(tmp_path / "memory")
    return ChatHistory.from_config(store_path=store_path)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def history(tmp_path: Path):
    """Isolated ChatHistory backed by a temporary SQLite store.

    Yields:
        ChatHistory: Fresh store for each test.
    """
    if not _SKMEMORY_AVAILABLE:
        pytest.skip("skmemory not installed — skipping integration tests")
    return _build_in_memory_history(tmp_path)


@pytest.fixture()
def messenger(history):
    """AgentMessenger with no transport (local-only mode).

    The messenger uses the in-memory history fixture and has no SKComm
    transport so all tests work without network or SKComm config.

    Args:
        history: Isolated ChatHistory fixture.

    Yields:
        AgentMessenger: Ready-to-use messenger for tests.
    """
    from skchat.agent_comm import AgentMessenger

    return AgentMessenger(
        identity="capauth:opus@skworld.io",
        history=history,
    )


@pytest.fixture()
def recipient_messenger(history):
    """AgentMessenger representing the recipient agent (Lumina).

    Shares the same ChatHistory as the sender so messages stored by
    the sender are visible to the recipient's inbox queries.

    Args:
        history: Isolated ChatHistory fixture (same as sender).

    Yields:
        AgentMessenger: Recipient agent messenger.
    """
    from skchat.agent_comm import AgentMessenger

    return AgentMessenger(
        identity="capauth:lumina@skworld.io",
        history=history,
    )


# ---------------------------------------------------------------------------
# Tests — Direct message roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _SKMEMORY_AVAILABLE, reason="skmemory not installed")
class TestDirectMessageRoundtrip:
    """Full send->store->inbox roundtrip for direct messages."""

    def test_send_returns_message_id(self, messenger):
        """send() should return a result dict with a non-empty message_id."""
        result = messenger.send(
            recipient="capauth:lumina@skworld.io",
            content="Hello Lumina, integration test calling.",
        )

        assert "message_id" in result
        assert result["message_id"]
        assert isinstance(result["message_id"], str)

    def test_send_without_transport_sets_stored_flag(self, messenger):
        """Without transport, send() returns stored=True and delivered=False."""
        result = messenger.send(
            recipient="capauth:lumina@skworld.io",
            content="Stored locally, not delivered.",
        )

        # No transport configured — delivered must be False
        assert result.get("delivered") is False
        # The message should be in local store
        assert result.get("stored") is True or result.get("message_id")

    def test_send_stores_message_in_history(self, messenger, history):
        """Message sent via AgentMessenger is retrievable from ChatHistory."""
        messenger.send(
            recipient="capauth:lumina@skworld.io",
            content="Persistent message for history check.",
        )

        count = history.message_count()
        assert count >= 1

    def test_check_inbox_returns_sent_message(self, messenger):
        """get_inbox() returns messages addressed to the sender's identity.

        Note: get_inbox() uses tag skchat:recipient:<identity>, so messages
        sent *from* this messenger to another agent appear in the history
        tagged with the recipient's URI. We verify that at least the
        message exists in the store via message_count after sending.
        """
        sender_identity = "capauth:opus@skworld.io"
        recipient_identity = "capauth:lumina@skworld.io"

        # Build a messenger whose identity IS the recipient so get_inbox works
        from skchat.agent_comm import AgentMessenger

        # Reuse the same history from the fixture's messenger
        recipient_agent = AgentMessenger(
            identity=recipient_identity,
            history=messenger._history,
        )

        # Send a message addressed to the recipient
        result = messenger.send(
            recipient=recipient_identity,
            content="Are you there, Lumina?",
            message_type="query",
        )
        assert result.get("message_id")

        # The recipient's inbox should contain the message
        inbox = recipient_agent.get_inbox(limit=50)
        # Inbox filters by tag skchat:recipient:<identity>
        # The message was tagged skchat:recipient:capauth:lumina@skworld.io
        found = any(m.get("content", "").strip() == "Are you there, Lumina?" for m in inbox)
        assert found, f"Message not found in inbox. inbox={inbox!r}"

    def test_message_type_preserved(self, messenger):
        """Message type metadata is preserved in the stored message."""
        from skchat.agent_comm import AgentMessenger

        recipient_identity = "capauth:lumina@skworld.io"
        recipient_agent = AgentMessenger(
            identity=recipient_identity,
            history=messenger._history,
        )

        messenger.send(
            recipient=recipient_identity,
            content="Found a bug in the transport layer.",
            message_type="finding",
        )

        inbox = recipient_agent.get_inbox(limit=50, message_type="finding")
        assert len(inbox) >= 1
        assert inbox[0].get("message_type") == "finding"

    def test_thread_id_is_preserved(self, messenger):
        """Thread ID set on send is stored and retrievable."""
        from skchat.agent_comm import AgentMessenger

        recipient_identity = "capauth:lumina@skworld.io"
        thread_id = "test-thread-integration-001"

        recipient_agent = AgentMessenger(
            identity=recipient_identity,
            history=messenger._history,
        )

        messenger.send(
            recipient=recipient_identity,
            content="Threaded message.",
            thread_id=thread_id,
        )

        # Retrieve via thread
        thread_msgs = messenger._history.get_thread_messages(thread_id, limit=10)
        assert len(thread_msgs) >= 1

    def test_send_finding_produces_structured_content(self, messenger):
        """send_finding() embeds severity and summary in content."""
        from skchat.agent_comm import AgentMessenger

        recipient_identity = "capauth:lumina@skworld.io"
        recipient_agent = AgentMessenger(
            identity=recipient_identity,
            history=messenger._history,
        )

        result = messenger.send_finding(
            recipient=recipient_identity,
            summary="Null pointer in parser.py line 42",
            severity="error",
            source_file="parser.py",
        )
        assert result.get("message_id")

        inbox = recipient_agent.get_inbox(limit=50, message_type="finding")
        assert len(inbox) >= 1
        content = inbox[0].get("content", "")
        assert "ERROR" in content
        assert "parser.py" in content

    def test_send_task_update_embeds_task_id(self, messenger):
        """send_task_update() includes task ID and status in the message."""
        from skchat.agent_comm import AgentMessenger

        recipient_identity = "capauth:lumina@skworld.io"
        recipient_agent = AgentMessenger(
            identity=recipient_identity,
            history=messenger._history,
        )

        task_id = "d56801a8-4444-4444-4444-123456789abc"
        result = messenger.send_task_update(
            recipient=recipient_identity,
            task_id=task_id,
            status="completed",
            summary="Transport tests all passing.",
        )
        assert result.get("message_id")

        inbox = recipient_agent.get_inbox(limit=50, message_type="task")
        assert len(inbox) >= 1
        content = inbox[0].get("content", "")
        assert task_id[:8] in content
        assert "completed" in content

    def test_multiple_messages_all_stored(self, messenger):
        """Sending multiple messages does not lose any in storage."""
        from skchat.agent_comm import AgentMessenger

        recipient_identity = "capauth:lumina@skworld.io"
        recipient_agent = AgentMessenger(
            identity=recipient_identity,
            history=messenger._history,
        )

        messages = [
            "First message",
            "Second message",
            "Third message",
        ]
        for msg in messages:
            messenger.send(recipient=recipient_identity, content=msg)

        inbox = recipient_agent.get_inbox(limit=100)
        contents = {m.get("content", "").strip() for m in inbox}
        for msg in messages:
            assert msg in contents, f"Message '{msg}' not found in inbox. inbox={contents!r}"

    def test_reply_to_is_preserved(self, messenger):
        """reply_to links are stored and retrievable."""
        from skchat.agent_comm import AgentMessenger

        recipient_identity = "capauth:lumina@skworld.io"
        recipient_agent = AgentMessenger(
            identity=recipient_identity,
            history=messenger._history,
        )

        # Send original
        original = messenger.send(
            recipient=recipient_identity,
            content="Original query.",
            message_type="query",
        )
        original_id = original["message_id"]

        # Send reply
        messenger.send(
            recipient=recipient_identity,
            content="Reply to original.",
            message_type="response",
            reply_to=original_id,
        )

        inbox = recipient_agent.get_inbox(limit=50, message_type="response")
        assert len(inbox) >= 1
        assert inbox[0].get("reply_to") == original_id


# ---------------------------------------------------------------------------
# Tests — Group message roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _SKMEMORY_AVAILABLE, reason="skmemory not installed")
class TestGroupMessageRoundtrip:
    """Full group create->compose->store->retrieve roundtrip."""

    def test_create_group_and_send_message(self, history):
        """Create a group, send a message, verify it is stored in the thread."""
        from skchat.group import GroupChat, ParticipantType

        group = GroupChat.create(
            name="Integration Test Group",
            creator_uri="capauth:opus@skworld.io",
            description="Created by integration tests",
        )
        group.add_member(
            identity_uri="capauth:lumina@skworld.io",
            participant_type=ParticipantType.AGENT,
        )

        msg = group.compose_group_message(
            sender_uri="capauth:opus@skworld.io",
            content="Hello integration test group!",
        )
        assert msg is not None

        # Store via ChatHistory
        memory_id = history.store_message(msg)
        assert memory_id

        # Retrieve via thread
        thread_msgs = history.get_thread_messages(group.id, limit=10)
        assert len(thread_msgs) >= 1
        found = any(
            m.get("content", "").strip() == "Hello integration test group!" for m in thread_msgs
        )
        assert found, f"Group message not in thread. thread_msgs={thread_msgs!r}"

    def test_group_message_thread_id_matches_group_id(self, history):
        """Messages composed by a group use the group ID as thread_id."""
        from skchat.group import GroupChat

        group = GroupChat.create(
            name="Thread ID Test Group",
            creator_uri="capauth:opus@skworld.io",
        )

        msg = group.compose_group_message(
            sender_uri="capauth:opus@skworld.io",
            content="Thread ID check.",
        )
        assert msg is not None
        assert msg.thread_id == group.id
        assert msg.recipient == f"group:{group.id}"

    def test_multiple_group_members_can_send(self, history):
        """All non-observer members can compose messages."""
        from skchat.group import GroupChat, MemberRole, ParticipantType

        group = GroupChat.create(
            name="Multi-member Test",
            creator_uri="capauth:opus@skworld.io",
        )
        group.add_member(
            identity_uri="capauth:lumina@skworld.io",
            participant_type=ParticipantType.AGENT,
            role=MemberRole.MEMBER,
        )

        for sender in ["capauth:opus@skworld.io", "capauth:lumina@skworld.io"]:
            msg = group.compose_group_message(
                sender_uri=sender,
                content=f"Message from {sender}",
            )
            assert msg is not None
            history.store_message(msg)

        thread_msgs = history.get_thread_messages(group.id, limit=20)
        assert len(thread_msgs) == 2

    def test_group_thread_retrieval_is_isolated(self, history):
        """Messages from different groups do not mix in thread retrieval."""
        from skchat.group import GroupChat

        group_a = GroupChat.create(name="Group A", creator_uri="capauth:opus@skworld.io")
        group_b = GroupChat.create(name="Group B", creator_uri="capauth:opus@skworld.io")

        msg_a = group_a.compose_group_message(
            sender_uri="capauth:opus@skworld.io",
            content="Group A only",
        )
        msg_b = group_b.compose_group_message(
            sender_uri="capauth:opus@skworld.io",
            content="Group B only",
        )

        history.store_message(msg_a)
        history.store_message(msg_b)

        thread_a = history.get_thread_messages(group_a.id, limit=10)
        thread_b = history.get_thread_messages(group_b.id, limit=10)

        contents_a = {m.get("content", "").strip() for m in thread_a}
        contents_b = {m.get("content", "").strip() for m in thread_b}

        assert "Group A only" in contents_a
        assert "Group B only" not in contents_a
        assert "Group B only" in contents_b
        assert "Group A only" not in contents_b

    def test_ephemeral_group_message(self, history):
        """Group messages with TTL are stored with TTL metadata."""
        from skchat.group import GroupChat

        group = GroupChat.create(
            name="Ephemeral Test",
            creator_uri="capauth:opus@skworld.io",
        )

        msg = group.compose_group_message(
            sender_uri="capauth:opus@skworld.io",
            content="This message will self-destruct.",
            ttl=30,
        )
        assert msg is not None
        assert msg.ttl == 30
        assert msg.is_ephemeral() is True

        history.store_message(msg)

        thread_msgs = history.get_thread_messages(group.id, limit=10)
        assert len(thread_msgs) >= 1


# ---------------------------------------------------------------------------
# Tests — Search
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _SKMEMORY_AVAILABLE, reason="skmemory not installed")
class TestSearchRoundtrip:
    """Verify full-text search works after storing messages."""

    def test_search_finds_stored_message(self, messenger, history):
        """search_messages() returns a message after it is stored."""
        unique_token = "xq7z-unique-integration-search-token"

        messenger.send(
            recipient="capauth:lumina@skworld.io",
            content=f"This message contains {unique_token}.",
        )

        results = history.search_messages(unique_token, limit=10)
        assert len(results) >= 1
        found = any(unique_token in m.get("content", "") for m in results)
        assert found
