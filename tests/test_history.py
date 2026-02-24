"""Tests for SKChat history — ChatHistory backed by SKMemory."""

from __future__ import annotations

from typing import Any, Optional

import pytest

from skchat.history import ChatHistory
from skchat.models import ChatMessage, ContentType, Thread


class FakeMemory:
    """Minimal Memory-like object for testing without real SKMemory."""

    def __init__(
        self,
        id: str,
        title: str,
        content: str,
        tags: list[str],
        metadata: dict[str, Any],
        created_at: str = "2026-02-23T00:00:00+00:00",
    ) -> None:
        self.id = id
        self.title = title
        self.content = content
        self.tags = tags
        self.metadata = metadata
        self.created_at = created_at


class FakeMemoryStore:
    """In-memory fake of SKMemory's MemoryStore for unit testing.

    Implements the subset of the MemoryStore API that ChatHistory uses.
    """

    def __init__(self) -> None:
        self._memories: list[FakeMemory] = []
        self._counter: int = 0

    def snapshot(
        self,
        title: str,
        content: str,
        tags: Optional[list[str]] = None,
        source: str = "manual",
        source_ref: str = "",
        metadata: Optional[dict] = None,
        **kwargs: Any,
    ) -> FakeMemory:
        """Store a fake memory.

        Args:
            title: Memory title.
            content: Memory content.
            tags: Tags for filtering.
            source: Source identifier.
            source_ref: Source reference.
            metadata: Key-value metadata.
            **kwargs: Ignored extra arguments.

        Returns:
            FakeMemory: The stored fake memory.
        """
        self._counter += 1
        mem = FakeMemory(
            id=f"mem-{self._counter}",
            title=title,
            content=content,
            tags=tags or [],
            metadata=metadata or {},
        )
        self._memories.append(mem)
        return mem

    def list_memories(
        self,
        tags: Optional[list[str]] = None,
        limit: int = 50,
        **kwargs: Any,
    ) -> list[FakeMemory]:
        """List fake memories with optional tag filtering.

        Args:
            tags: Filter by tags (AND logic).
            limit: Maximum results.
            **kwargs: Ignored extra arguments.

        Returns:
            list[FakeMemory]: Matching memories.
        """
        results = []
        for mem in self._memories:
            if tags and not all(t in mem.tags for t in tags):
                continue
            results.append(mem)
        return results[:limit]

    def search(self, query: str, limit: int = 10) -> list[FakeMemory]:
        """Search fake memories by content substring.

        Args:
            query: Search substring.
            limit: Maximum results.

        Returns:
            list[FakeMemory]: Matching memories.
        """
        results = [m for m in self._memories if query.lower() in m.content.lower()]
        return results[:limit]


@pytest.fixture()
def fake_store() -> FakeMemoryStore:
    """Create a fresh FakeMemoryStore.

    Returns:
        FakeMemoryStore: Empty in-memory store.
    """
    return FakeMemoryStore()


@pytest.fixture()
def history(fake_store: FakeMemoryStore) -> ChatHistory:
    """Create a ChatHistory backed by a FakeMemoryStore.

    Args:
        fake_store: The fake store fixture.

    Returns:
        ChatHistory: Ready-to-use chat history.
    """
    return ChatHistory(store=fake_store)


class TestChatHistory:
    """Tests for ChatHistory message persistence."""

    def test_store_message(self, history: ChatHistory) -> None:
        """Happy path: store a message and get a memory ID back."""
        msg = ChatMessage(
            sender="capauth:alice@skworld.io",
            recipient="capauth:bob@skworld.io",
            content="Hello Bob!",
            content_type=ContentType.PLAIN,
        )
        mem_id = history.store_message(msg)
        assert mem_id is not None
        assert mem_id.startswith("mem-")

    def test_store_message_with_thread(self, history: ChatHistory) -> None:
        """Messages with thread_id get the thread tag."""
        msg = ChatMessage(
            sender="capauth:alice@skworld.io",
            recipient="capauth:bob@skworld.io",
            content="Thread message",
            thread_id="thread-abc",
        )
        history.store_message(msg)
        assert history.message_count() == 1

    def test_store_thread(self, history: ChatHistory) -> None:
        """Happy path: store a thread's metadata."""
        thread = Thread(
            title="Dev Chat",
            participants=["capauth:alice@skworld.io", "capauth:bob@skworld.io"],
        )
        mem_id = history.store_thread(thread)
        assert mem_id is not None

    def test_get_thread_messages(self, history: ChatHistory) -> None:
        """Retrieve messages from a specific thread."""
        thread_id = "thread-xyz"
        for i in range(3):
            msg = ChatMessage(
                sender="capauth:alice@skworld.io",
                recipient="capauth:bob@skworld.io",
                content=f"Message {i}",
                thread_id=thread_id,
            )
            history.store_message(msg)

        unrelated = ChatMessage(
            sender="capauth:alice@skworld.io",
            recipient="capauth:bob@skworld.io",
            content="Not in thread",
        )
        history.store_message(unrelated)

        thread_msgs = history.get_thread_messages(thread_id)
        assert len(thread_msgs) == 3

    def test_search_messages(self, history: ChatHistory) -> None:
        """Full-text search across messages."""
        msg1 = ChatMessage(
            sender="capauth:alice@skworld.io",
            recipient="capauth:bob@skworld.io",
            content="The quantum upgrade is ready",
        )
        msg2 = ChatMessage(
            sender="capauth:bob@skworld.io",
            recipient="capauth:alice@skworld.io",
            content="Deploy the new build",
        )
        history.store_message(msg1)
        history.store_message(msg2)

        results = history.search_messages("quantum")
        assert len(results) == 1
        assert "quantum" in results[0]["content"].lower()

    def test_search_no_results(self, history: ChatHistory) -> None:
        """Search with no matches returns empty list."""
        results = history.search_messages("nonexistent-query")
        assert results == []

    def test_message_count(self, history: ChatHistory) -> None:
        """message_count tracks total stored messages."""
        assert history.message_count() == 0

        for i in range(5):
            msg = ChatMessage(
                sender="capauth:alice@skworld.io",
                recipient="capauth:bob@skworld.io",
                content=f"Msg {i}",
            )
            history.store_message(msg)

        assert history.message_count() == 5

    def test_list_threads(self, history: ChatHistory) -> None:
        """List all stored threads."""
        for i in range(3):
            thread = Thread(
                title=f"Thread {i}",
                participants=["capauth:alice@skworld.io"],
            )
            history.store_thread(thread)

        threads = history.list_threads()
        assert len(threads) == 3

    def test_memory_to_chat_dict(self, history: ChatHistory) -> None:
        """The dict representation has expected chat fields."""
        msg = ChatMessage(
            sender="capauth:alice@skworld.io",
            recipient="capauth:bob@skworld.io",
            content="Dict test",
            thread_id="thread-dict",
        )
        history.store_message(msg)

        thread_msgs = history.get_thread_messages("thread-dict")
        assert len(thread_msgs) == 1
        d = thread_msgs[0]
        assert d["sender"] == "capauth:alice@skworld.io"
        assert d["recipient"] == "capauth:bob@skworld.io"
        assert d["content"] == "Dict test"
        assert d["thread_id"] == "thread-dict"
