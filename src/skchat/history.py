"""ChatHistory — persistent chat storage backed by SKMemory.

Every chat message is stored as an SKMemory Memory object, which gives
us emotional context, vector search, and the full memory lifecycle
(short-term -> mid-term -> long-term) for free.

ChatHistory is the bridge between the ephemeral ChatMessage model
and the persistent MemoryStore.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .models import ChatMessage, Thread


class ChatHistory:
    """Persistent chat history backed by SKMemory's MemoryStore.

    Stores messages as Memory objects with chat-specific tags and metadata.
    Provides conversation-oriented retrieval (by thread, by participant,
    by time range) on top of SKMemory's generic search.

    Args:
        store: An SKMemory MemoryStore instance for persistence.
    """

    CHAT_TAG = "skchat"
    MESSAGE_TAG = "skchat:message"
    THREAD_TAG_PREFIX = "skchat:thread:"

    def __init__(self, store: object) -> None:
        self._store = store

    @classmethod
    def from_config(cls, store_path: Optional[str] = None) -> "ChatHistory":
        """Create a ChatHistory from config, backed by SKMemory SQLite.

        Args:
            store_path: Override store directory. Defaults to ~/.skchat/memory/.

        Returns:
            ChatHistory backed by persistent SQLite storage.
        """
        from pathlib import Path

        if store_path is None:
            store_path = str(Path("~/.skchat/memory").expanduser())

        Path(store_path).mkdir(parents=True, exist_ok=True)

        try:
            from skmemory import MemoryStore, SQLiteBackend

            backend = SQLiteBackend(base_path=store_path)
            store = MemoryStore(primary=backend)
        except ImportError:
            from skmemory import MemoryStore

            store = MemoryStore()

        return cls(store=store)

    def store_message(self, message: ChatMessage) -> str:
        """Store a chat message as an SKMemory memory.

        Converts the ChatMessage into a Memory snapshot with chat-specific
        tags for later retrieval. Plaintext content is stored; encryption
        should be handled before transport, not at the storage layer.

        Args:
            message: The ChatMessage to persist.

        Returns:
            str: The memory ID assigned to this message.
        """
        tags = [self.CHAT_TAG, self.MESSAGE_TAG]
        if message.thread_id:
            tags.append(f"{self.THREAD_TAG_PREFIX}{message.thread_id}")
        tags.append(f"skchat:sender:{message.sender}")
        tags.append(f"skchat:recipient:{message.recipient}")

        metadata = {
            "chat_message_id": message.id,
            "sender": message.sender,
            "recipient": message.recipient,
            "content_type": message.content_type.value,
            "thread_id": message.thread_id,
            "reply_to": message.reply_to,
            "delivery_status": message.delivery_status.value,
            "ttl": message.ttl,
        }
        metadata.update(message.metadata)

        title = message.to_summary()

        memory = self._store.snapshot(
            title=title,
            content=message.content,
            tags=tags,
            source="skchat",
            source_ref=message.id,
            metadata=metadata,
        )
        return memory.id

    def store_thread(self, thread: Thread) -> str:
        """Store a thread's metadata as an SKMemory memory.

        Args:
            thread: The Thread to persist.

        Returns:
            str: The memory ID assigned to this thread record.
        """
        tags = [self.CHAT_TAG, "skchat:thread_meta"]
        tags.append(f"{self.THREAD_TAG_PREFIX}{thread.id}")

        title = thread.title or f"Thread {thread.id[:8]}"
        content = (
            f"Thread: {title}\n"
            f"Participants: {', '.join(thread.participants)}\n"
            f"Messages: {thread.message_count}\n"
            f"Created: {thread.created_at.isoformat()}\n"
            f"Updated: {thread.updated_at.isoformat()}"
        )

        metadata = {
            "thread_id": thread.id,
            "participants": thread.participants,
            "message_count": thread.message_count,
            "parent_thread_id": thread.parent_thread_id,
        }
        metadata.update(thread.metadata)

        memory = self._store.snapshot(
            title=title,
            content=content,
            tags=tags,
            source="skchat",
            source_ref=f"thread:{thread.id}",
            metadata=metadata,
        )
        return memory.id

    def get_thread_messages(
        self,
        thread_id: str,
        limit: int = 50,
    ) -> list[dict]:
        """Retrieve messages from a specific thread.

        Args:
            thread_id: The thread identifier.
            limit: Maximum messages to return.

        Returns:
            list[dict]: Memory dicts with chat metadata, newest first.
        """
        tag = f"{self.THREAD_TAG_PREFIX}{thread_id}"
        memories = self._store.list_memories(tags=[tag], limit=limit)
        return [
            self._memory_to_chat_dict(m)
            for m in memories
            if self.MESSAGE_TAG in m.tags
        ]

    def get_conversation(
        self,
        participant_a: str,
        participant_b: str,
        limit: int = 50,
    ) -> list[dict]:
        """Retrieve direct messages between two participants.

        Uses SKMemory search to find messages tagged with both participants.

        Args:
            participant_a: CapAuth identity URI of first participant.
            participant_b: CapAuth identity URI of second participant.
            limit: Maximum messages to return.

        Returns:
            list[dict]: Memory dicts for the conversation.
        """
        tag_a = f"skchat:sender:{participant_a}"
        tag_b = f"skchat:sender:{participant_b}"

        # Reason: SKMemory list_memories uses AND for tags, so we search
        # for each direction separately and merge by timestamp
        sent = self._store.list_memories(
            tags=[self.MESSAGE_TAG, tag_a],
            limit=limit,
        )
        received = self._store.list_memories(
            tags=[self.MESSAGE_TAG, tag_b],
            limit=limit,
        )

        all_messages = []
        seen_ids: set[str] = set()
        for m in sent + received:
            if m.id in seen_ids:
                continue
            seen_ids.add(m.id)
            meta = m.metadata
            a_involved = (
                meta.get("sender") in (participant_a, participant_b)
                and meta.get("recipient") in (participant_a, participant_b)
            )
            if a_involved:
                all_messages.append(self._memory_to_chat_dict(m))

        all_messages.sort(key=lambda d: d.get("timestamp", ""), reverse=True)
        return all_messages[:limit]

    def search_messages(self, query: str, limit: int = 20) -> list[dict]:
        """Full-text search across all chat messages.

        Leverages SKMemory's search (vector or text) with chat filtering.

        Args:
            query: Search query string.
            limit: Maximum results.

        Returns:
            list[dict]: Matching message dicts ranked by relevance.
        """
        results = self._store.search(query, limit=limit * 2)
        chat_results = [
            self._memory_to_chat_dict(m)
            for m in results
            if self.CHAT_TAG in m.tags and self.MESSAGE_TAG in m.tags
        ]
        return chat_results[:limit]

    def get_thread(self, thread_id: str) -> Optional[dict]:
        """Retrieve a specific thread's full metadata by ID.

        Args:
            thread_id: The thread identifier.

        Returns:
            Optional[dict]: Full thread metadata including all stored fields,
                or None if not found.
        """
        tag = f"{self.THREAD_TAG_PREFIX}{thread_id}"
        memories = self._store.list_memories(
            tags=["skchat:thread_meta", tag],
            limit=1,
        )
        if not memories:
            return None
        m = memories[0]
        result = {
            "thread_id": m.metadata.get("thread_id"),
            "title": m.title,
            "participants": m.metadata.get("participants", []),
            "message_count": m.metadata.get("message_count", 0),
            "parent_thread_id": m.metadata.get("parent_thread_id"),
        }
        result.update(m.metadata)
        return result

    def list_threads(self, limit: int = 50) -> list[dict]:
        """List all known chat threads.

        Args:
            limit: Maximum threads to return.

        Returns:
            list[dict]: Thread metadata dicts.
        """
        memories = self._store.list_memories(
            tags=["skchat:thread_meta"],
            limit=limit,
        )
        return [
            {
                "thread_id": m.metadata.get("thread_id"),
                "title": m.title,
                "participants": m.metadata.get("participants", []),
                "message_count": m.metadata.get("message_count", 0),
                "parent_thread_id": m.metadata.get("parent_thread_id"),
            }
            for m in memories
        ]

    def message_count(self) -> int:
        """Count total stored chat messages.

        Returns:
            int: Number of chat messages in memory.
        """
        messages = self._store.list_memories(
            tags=[self.MESSAGE_TAG],
            limit=10000,
        )
        return len(messages)

    @staticmethod
    def _memory_to_chat_dict(memory: object) -> dict:
        """Convert an SKMemory Memory back to a chat-oriented dict.

        Args:
            memory: An SKMemory Memory object.

        Returns:
            dict: Chat message representation.
        """
        return {
            "memory_id": memory.id,
            "chat_message_id": memory.metadata.get("chat_message_id"),
            "sender": memory.metadata.get("sender"),
            "recipient": memory.metadata.get("recipient"),
            "content": memory.content,
            "content_type": memory.metadata.get("content_type"),
            "thread_id": memory.metadata.get("thread_id"),
            "reply_to": memory.metadata.get("reply_to"),
            "delivery_status": memory.metadata.get("delivery_status"),
            "timestamp": memory.created_at,
            "tags": memory.tags,
        }
