"""Tests for SKChat history — ChatHistory backed by SKMemory."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import MagicMock

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


def _write_msg(history_dir, sender, recipient, content, iso_ts) -> ChatMessage:
    """Append a ChatMessage to the dated JSONL file matching its timestamp.

    Writing to the date-named file directly (rather than via save(), which
    always uses *today*) makes since/prune date filtering deterministic.
    """
    ts = datetime.fromisoformat(iso_ts)
    msg = ChatMessage(sender=sender, recipient=recipient, content=content, timestamp=ts)
    path = history_dir / f"{ts.strftime('%Y-%m-%d')}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(msg.model_dump_json())
        fh.write("\n")
    return msg


@pytest.fixture()
def jsonl_history(tmp_path):
    """A ChatHistory whose JSONL store lives in a fresh tmp dir."""
    hist_dir = tmp_path / "history"
    hist_dir.mkdir()
    hist = ChatHistory(store=MagicMock(), history_dir=hist_dir)
    return hist, hist_dir


class TestChatHistorySince:
    """The load() since= filter (audit: already present — these lock it in)."""

    def test_since_filters_older_messages(self, jsonl_history) -> None:
        hist, hist_dir = jsonl_history
        _write_msg(hist_dir, "a", "b", "old", "2026-02-20T10:00:00+00:00")
        _write_msg(hist_dir, "a", "b", "new", "2026-02-25T10:00:00+00:00")

        cutoff = datetime(2026, 2, 23, tzinfo=timezone.utc)
        results = hist.load(since=cutoff)
        contents = [m.content for m in results]
        assert "new" in contents
        assert "old" not in contents

    def test_since_naive_treated_as_utc(self, jsonl_history) -> None:
        hist, hist_dir = jsonl_history
        _write_msg(hist_dir, "a", "b", "keep", "2026-02-25T10:00:00+00:00")
        # naive cutoff — must be coerced to UTC, not crash
        results = hist.load(since=datetime(2026, 2, 24))
        assert [m.content for m in results] == ["keep"]


class TestChatHistoryPrune:
    """Tests for prune(before=...)."""

    def test_prune_removes_old_messages(self, jsonl_history) -> None:
        hist, hist_dir = jsonl_history
        _write_msg(hist_dir, "a", "b", "old1", "2026-02-20T10:00:00+00:00")
        _write_msg(hist_dir, "a", "b", "old2", "2026-02-21T10:00:00+00:00")
        _write_msg(hist_dir, "a", "b", "fresh", "2026-02-25T10:00:00+00:00")

        removed = hist.prune(before=datetime(2026, 2, 23, tzinfo=timezone.utc))
        assert removed == 2
        remaining = [m.content for m in hist.load()]
        assert remaining == ["fresh"]

    def test_prune_empty_history_noop(self, jsonl_history) -> None:
        hist, _ = jsonl_history
        assert hist.prune(before=datetime(2026, 2, 23, tzinfo=timezone.utc)) == 0

    def test_prune_keeps_everything_when_cutoff_is_old(self, jsonl_history) -> None:
        hist, hist_dir = jsonl_history
        _write_msg(hist_dir, "a", "b", "m1", "2026-02-25T10:00:00+00:00")
        _write_msg(hist_dir, "a", "b", "m2", "2026-02-26T10:00:00+00:00")
        removed = hist.prune(before=datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert removed == 0
        assert len(hist.load()) == 2


class TestChatHistoryGetUnread:
    """Tests for get_unread(last_read=...)."""

    def test_unread_returns_messages_after_cursor(self, jsonl_history) -> None:
        hist, hist_dir = jsonl_history
        _write_msg(hist_dir, "a", "b", "seen", "2026-02-20T10:00:00+00:00")
        _write_msg(hist_dir, "a", "b", "unseen", "2026-02-25T10:00:00+00:00")

        cursor = datetime(2026, 2, 22, tzinfo=timezone.utc)
        unread = hist.get_unread(last_read=cursor)
        assert [m.content for m in unread] == ["unseen"]

    def test_unread_none_cursor_returns_all(self, jsonl_history) -> None:
        hist, hist_dir = jsonl_history
        _write_msg(hist_dir, "a", "b", "m1", "2026-02-20T10:00:00+00:00")
        _write_msg(hist_dir, "a", "b", "m2", "2026-02-25T10:00:00+00:00")
        unread = hist.get_unread(last_read=None)
        assert len(unread) == 2

    def test_unread_filters_by_peer(self, jsonl_history) -> None:
        hist, hist_dir = jsonl_history
        _write_msg(hist_dir, "alice", "me", "from alice", "2026-02-25T10:00:00+00:00")
        _write_msg(hist_dir, "bob", "me", "from bob", "2026-02-25T11:00:00+00:00")
        unread = hist.get_unread(last_read=None, peer="alice")
        assert [m.content for m in unread] == ["from alice"]

    def test_unread_excludes_boundary_equal_cursor(self, jsonl_history) -> None:
        hist, hist_dir = jsonl_history
        _write_msg(hist_dir, "a", "b", "exact", "2026-02-25T10:00:00+00:00")
        cursor = datetime(2026, 2, 25, 10, 0, 0, tzinfo=timezone.utc)
        # strictly-after semantics: a message AT the cursor is already read
        assert hist.get_unread(last_read=cursor) == []


# ---------------------------------------------------------------------------
# QA additions — JSONL save/load round-trip + store-backed retrieval helpers
# ---------------------------------------------------------------------------


class TestJsonlSaveLoad:
    def test_save_then_load_round_trip(self, jsonl_history) -> None:
        hist, _ = jsonl_history
        msg = hist.add_message("capauth:a@x", "capauth:b@x", "hello jsonl")
        loaded = hist.load()
        assert len(loaded) == 1
        assert loaded[0].id == msg.id
        assert loaded[0].content == "hello jsonl"

    def test_load_peer_filter(self, jsonl_history) -> None:
        hist, hist_dir = jsonl_history
        _write_msg(hist_dir, "alice", "me", "from alice", "2026-02-25T10:00:00+00:00")
        _write_msg(hist_dir, "bob", "me", "from bob", "2026-02-25T11:00:00+00:00")
        assert [m.content for m in hist.load(peer="alice")] == ["from alice"]

    def test_load_respects_limit(self, jsonl_history) -> None:
        hist, hist_dir = jsonl_history
        for i in range(5):
            _write_msg(hist_dir, "a", "b", f"m{i}", f"2026-02-2{i}T10:00:00+00:00")
        assert len(hist.load(limit=2)) == 2

    def test_load_skips_malformed_lines(self, jsonl_history) -> None:
        hist, hist_dir = jsonl_history
        path = hist_dir / "2026-02-25.jsonl"
        good = _write_msg(hist_dir, "a", "b", "good", "2026-02-25T10:00:00+00:00")
        with path.open("a", encoding="utf-8") as fh:
            fh.write("not json at all\n")
        loaded = hist.load()
        assert [m.id for m in loaded] == [good.id]

    def test_add_message_returns_and_persists(self, jsonl_history) -> None:
        hist, _ = jsonl_history
        msg = hist.add_message("a", "b", "c")
        assert isinstance(msg, ChatMessage)
        assert len(hist.load()) == 1

    def test_get_messages_dict_shape(self, jsonl_history) -> None:
        hist, hist_dir = jsonl_history
        _write_msg(hist_dir, "alice", "me", "yo", "2026-02-25T10:00:00+00:00")
        out = hist.get_messages(peer="alice")
        assert out[0]["sender"] == "alice"
        assert out[0]["content"] == "yo"
        assert isinstance(out[0]["timestamp"], str)  # ISO string

    def test_get_thread_jsonl_oldest_first(self, jsonl_history) -> None:
        """get_thread scans JSONL, filters by thread, returns oldest-first."""
        hist, hist_dir = jsonl_history
        for i, c in enumerate(["first", "second"]):
            ts = datetime.fromisoformat(f"2026-02-25T1{i}:00:00+00:00")
            m = ChatMessage(sender="a", recipient="b", content=c,
                            thread_id="t-thread", timestamp=ts)
            path = hist_dir / f"{ts.strftime('%Y-%m-%d')}.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(m.model_dump_json() + "\n")
        # An off-thread message that must be excluded.
        _write_msg(hist_dir, "a", "b", "other", "2026-02-25T12:00:00+00:00")
        thread = hist.get_thread("t-thread")
        assert [m.content for m in thread] == ["first", "second"]


class TestStoreBackedRetrieval:
    """Helpers that go through the SKMemory store, using the FakeMemoryStore."""

    def test_store_message_tags(self, history: ChatHistory, fake_store) -> None:
        msg = ChatMessage(sender="capauth:alice@x", recipient="capauth:bob@x",
                          content="tagged", thread_id="t1")
        history.store_message(msg)
        mem = fake_store._memories[0]
        assert "skchat" in mem.tags
        assert "skchat:message" in mem.tags
        assert "skchat:thread:t1" in mem.tags
        assert "skchat:sender:capauth:alice@x" in mem.tags
        assert "skchat:recipient:capauth:bob@x" in mem.tags
        assert mem.metadata["chat_message_id"] == msg.id

    def test_get_conversation_merges_both_directions(self, history: ChatHistory) -> None:
        a, b = "capauth:alice@x", "capauth:bob@x"
        history.store_message(ChatMessage(sender=a, recipient=b, content="a→b"))
        history.store_message(ChatMessage(sender=b, recipient=a, content="b→a"))
        # An unrelated message that must not appear.
        history.store_message(
            ChatMessage(sender=a, recipient="capauth:carol@x", content="a→c")
        )
        convo = history.get_conversation(a, b)
        contents = {m["content"] for m in convo}
        assert contents == {"a→b", "b→a"}

    def test_store_thread_metadata(self, history: ChatHistory, fake_store) -> None:
        thread = Thread(title="Topic", participants=["capauth:a@x", "capauth:b@x"])
        history.store_thread(thread)
        mem = fake_store._memories[0]
        assert mem.metadata["thread_id"] == thread.id
        assert mem.metadata["participants"] == ["capauth:a@x", "capauth:b@x"]
        assert "skchat:thread_meta" in mem.tags

    def test_get_thread_meta_found_and_missing(self, history: ChatHistory) -> None:
        thread = Thread(title="Topic", participants=["capauth:a@x"])
        history.store_thread(thread)
        meta = history.get_thread_meta(thread.id)
        assert meta is not None
        assert meta["title"] == "Topic"
        assert history.get_thread_meta("no-such-thread") is None

    def test_get_messages_since_store_none(self) -> None:
        """get_messages_since returns [] when there is no store at all."""
        from skchat.history import ChatHistory as CH
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            hist = CH(store=None, history_dir=d)
            hist._store = None  # force the no-store branch
            assert hist.get_messages_since(minutes=60) == []

    def test_get_messages_since_recipient_filter(self, history: ChatHistory) -> None:
        history.store_message(
            ChatMessage(sender="capauth:a@x", recipient="capauth:me@x", content="mine")
        )
        history.store_message(
            ChatMessage(sender="capauth:a@x", recipient="capauth:other@x", content="theirs")
        )
        # minutes=0 → no time filter (FakeMemory.created_at is a fixed past date).
        out = history.get_messages_since(minutes=0, recipient="capauth:me@x")
        assert [m["content"] for m in out] == ["mine"]
