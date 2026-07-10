"""ChatHistory — persistent chat storage.

Primary storage: append-only JSONL files at ~/.skchat/history/YYYY-MM-DD.jsonl,
one JSON line per ChatMessage (via model_dump_json()).

Optional SKMemory backend: when a MemoryStore is supplied the existing
vector-search / thread helpers remain available.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .models import ChatMessage, Thread

logger = logging.getLogger(__name__)


def _skchat_home() -> Path:
    """Resolve the skchat home dir, honouring ``SKCHAT_HOME``.

    Lets multiple agent instances keep SEPARATE stores on one box (e.g. an
    ``opus`` daemon + webui with ``SKCHAT_HOME=~/.skchat-opus``) without
    co-mingling messages with the default ``~/.skchat`` agent. When
    ``SKCHAT_HOME`` is unset the default is ``~/.skchat`` — unchanged behaviour.
    """
    return Path(os.environ.get("SKCHAT_HOME") or "~/.skchat").expanduser()


class ChatHistory:
    """Persistent chat history with JSONL file storage.

    JSONL files live at ~/.skchat/history/YYYY-MM-DD.jsonl.
    Each line is a JSON-serialised ChatMessage produced by
    ``message.model_dump_json()``.

    An optional SKMemory MemoryStore can be supplied for the legacy
    vector-search / thread helpers; it is not required for basic
    save/load operations.

    Args:
        store: Optional SKMemory MemoryStore instance.
        history_dir: Override the directory for JSONL files.
    """

    CHAT_TAG = "skchat"
    MESSAGE_TAG = "skchat:message"
    THREAD_TAG_PREFIX = "skchat:thread:"

    def __init__(
        self,
        store: object = None,
        history_dir: Optional[Path | str] = None,
    ) -> None:
        if store is None:
            store = self._make_default_store()
        self._store = store
        self._history_dir: Path = (
            Path(history_dir).expanduser()
            if history_dir is not None
            else _skchat_home() / "history"
        )
        self._history_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _make_default_store() -> object:
        """Create a default SQLite-backed MemoryStore at ~/.skchat/memory/.

        Called automatically when no store is supplied to __init__, so that
        ``ChatHistory()`` (no args) behaves the same as ``ChatHistory.from_config()``.

        Returns:
            MemoryStore backed by SQLite, or None if skmemory is unavailable.
        """
        store_path = str(_skchat_home() / "memory")
        Path(store_path).mkdir(parents=True, exist_ok=True)
        try:
            from skmemory import MemoryStore, SQLiteBackend

            backend = SQLiteBackend(base_path=store_path)
            return MemoryStore(primary=backend)
        except ImportError:
            try:
                from skmemory import MemoryStore

                return MemoryStore()
            except ImportError:
                return None

    # ------------------------------------------------------------------
    # JSONL save / load
    # ------------------------------------------------------------------

    def save(self, message: ChatMessage) -> None:
        """Append *message* to today's JSONL history file.

        File: ``~/.skchat/history/YYYY-MM-DD.jsonl``
        One JSON line per call, written atomically (append mode).

        Args:
            message: The ChatMessage to persist.
        """
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self._history_dir / f"{date_str}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(message.model_dump_json())
            fh.write("\n")

    def load(
        self,
        since: Optional[datetime] = None,
        peer: Optional[str] = None,
        limit: int = 100,
    ) -> list[ChatMessage]:
        """Load messages from JSONL history files.

        Reads all ``.jsonl`` files in the history directory in reverse
        chronological order (newest date first).  Each line is parsed as a
        :class:`ChatMessage`.  Malformed lines are silently skipped.

        Args:
            since: If given, only return messages with
                ``timestamp >= since``.  Naive datetimes are treated as UTC.
            peer: If given, only return messages where *peer* appears as
                either ``sender`` or ``recipient``.
            limit: Maximum number of messages to return.

        Returns:
            List of :class:`ChatMessage` objects, newest first, up to
            *limit* entries.
        """
        # Normalise `since` to an aware UTC datetime for safe comparison.
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)

        files = sorted(self._history_dir.glob("*.jsonl"), reverse=True)

        results: list[ChatMessage] = []
        for path in files:
            if len(results) >= limit:
                break
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            # Iterate newest-first within the file (reverse line order).
            for raw in reversed(lines):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = ChatMessage.model_validate_json(raw)
                except Exception as e:
                    logger.warning("history.py: %s", e)
                    continue

                # Filter by timestamp.
                if since is not None:
                    ts = msg.timestamp
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < since:
                        continue

                # Filter by peer (sender or recipient).
                if peer is not None and peer not in (msg.sender, msg.recipient):
                    continue

                results.append(msg)
                if len(results) >= limit:
                    break

        return results

    def get_unread(
        self,
        last_read: Optional[datetime] = None,
        peer: Optional[str] = None,
        limit: int = 1000,
    ) -> list[ChatMessage]:
        """Return messages newer than the *last_read* cursor.

        "Unread" follows the same convention the CLI inbox uses: a message is
        unread when its ``timestamp`` is strictly **after** the last-read
        marker.  A message exactly at the cursor is considered already read.
        Pass ``last_read=None`` to treat the whole history as unread.

        Args:
            last_read: Last-read timestamp cursor.  Naive datetimes are
                treated as UTC.  ``None`` returns all messages.
            peer: If given, only messages where *peer* is the ``sender`` or
                ``recipient``.
            limit: Maximum number of messages to return (newest first).

        Returns:
            list[ChatMessage]: Unread messages, newest first.
        """
        messages = self.load(peer=peer, limit=limit)
        if last_read is None:
            return messages

        if last_read.tzinfo is None:
            last_read = last_read.replace(tzinfo=timezone.utc)

        unread: list[ChatMessage] = []
        for msg in messages:
            ts = msg.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts > last_read:
                unread.append(msg)
        return unread

    def prune(self, before: datetime) -> int:
        """Delete history entries older than *before*.

        Rewrites each dated JSONL file, dropping every message whose
        ``timestamp`` is strictly before the cutoff.  Files left empty are
        removed.  Malformed lines are dropped (they cannot be dated safely).

        Args:
            before: Cutoff datetime.  Messages with ``timestamp < before`` are
                deleted.  Naive datetimes are treated as UTC.

        Returns:
            int: Number of messages removed.
        """
        if before.tzinfo is None:
            before = before.replace(tzinfo=timezone.utc)

        removed = 0
        for path in self._history_dir.glob("*.jsonl"):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue

            kept: list[str] = []
            for raw in lines:
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    msg = ChatMessage.model_validate_json(stripped)
                except Exception as e:
                    logger.warning("history.py prune: dropping malformed line: %s", e)
                    removed += 1
                    continue
                ts = msg.timestamp
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < before:
                    removed += 1
                else:
                    kept.append(stripped)

            if kept:
                path.write_text("\n".join(kept) + "\n", encoding="utf-8")
            else:
                # Nothing left in this file — remove it.
                try:
                    path.unlink()
                except OSError as e:
                    logger.warning("history.py prune: could not remove %s: %s", path, e)

        return removed

    # ------------------------------------------------------------------
    # In-place mutation (reactions / edits / receipts) — JSONL-safe
    # ------------------------------------------------------------------
    #
    # The JSONL store is append-only for *new* messages, but reactions, edits
    # and receipts mutate an EXISTING message. We rewrite only the dated file
    # that holds the target line — O(one day's file), migration-safe (any line
    # that doesn't parse, or isn't the target, is preserved byte-for-line).

    def find_by_id(self, message_id: str) -> Optional[ChatMessage]:
        """Return the stored message with *message_id*, or None.

        Scans newest-day-first (edits usually target recent messages).
        """
        files = sorted(self._history_dir.glob("*.jsonl"), reverse=True)
        for path in files:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for raw in reversed(lines):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = ChatMessage.model_validate_json(raw)
                except Exception:
                    continue
                if msg.id == message_id:
                    return msg
        return None

    def update_message(self, message: ChatMessage) -> bool:
        """Persist mutations to an existing message in place.

        Finds the JSONL line whose parsed ``id`` matches *message*'s id and
        rewrites just that line with the updated serialization. Other lines —
        including malformed/legacy ones — are preserved untouched.

        Args:
            message: The mutated :class:`ChatMessage` to write back.

        Returns:
            bool: True if a matching line was found and rewritten.
        """
        files = sorted(self._history_dir.glob("*.jsonl"), reverse=True)
        for path in files:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            replaced = False
            out: list[str] = []
            for raw in lines:
                stripped = raw.strip()
                if not stripped:
                    continue
                if not replaced:
                    try:
                        existing = ChatMessage.model_validate_json(stripped)
                    except Exception:
                        out.append(stripped)
                        continue
                    if existing.id == message.id:
                        out.append(message.model_dump_json())
                        replaced = True
                        continue
                out.append(stripped)
            if replaced:
                path.write_text("\n".join(out) + "\n", encoding="utf-8")
                return True
        return False

    def set_reaction(self, message_id: str, emoji: str, sender: str) -> Optional[ChatMessage]:
        """Add *sender*'s *emoji* reaction to a message and persist it.

        Idempotent: a duplicate (same sender+emoji) is a no-op but still
        returns the message. Returns None if the message is not found.
        """
        msg = self.find_by_id(message_id)
        if msg is None:
            return None
        if msg.set_reaction(emoji, sender):
            self.update_message(msg)
        return msg

    def clear_reaction(self, message_id: str, emoji: str, sender: str) -> Optional[ChatMessage]:
        """Remove *sender*'s *emoji* reaction and persist. None if not found."""
        msg = self.find_by_id(message_id)
        if msg is None:
            return None
        if msg.clear_reaction(emoji, sender):
            self.update_message(msg)
        return msg

    def edit_message(
        self, message_id: str, new_body: str, *, enforce_window: bool = True
    ) -> Optional[ChatMessage]:
        """Apply an edit (archives prior body, stamps edited_at) and persist.

        Enforces the 24h server-side edit window by default.

        Returns:
            The updated message, or None if not found.

        Raises:
            ValueError: If the edit window has elapsed or the body is empty.
        """
        msg = self.find_by_id(message_id)
        if msg is None:
            return None
        msg.apply_edit(new_body, enforce_window=enforce_window)
        self.update_message(msg)
        return msg

    def record_receipt(
        self, message_id: str, kind: str, sender: str
    ) -> Optional[ChatMessage]:
        """Record a delivered/read receipt for *sender* and persist.

        Idempotent. Returns None if the message is not found.
        """
        msg = self.find_by_id(message_id)
        if msg is None:
            return None
        if msg.record_receipt(kind, sender):
            self.update_message(msg)
        return msg

    def list_media(
        self,
        peer: str,
        *,
        kinds: tuple[str, ...] = ("image", "video"),
        limit: int = 200,
    ) -> list[dict]:
        """List media attachments exchanged with *peer*, newest-first.

        Read-only view over the JSONL history: loads messages involving *peer*
        (as sender or recipient), flattens their :class:`FileRef` attachments,
        and keeps those whose ``mime_type`` matches one of *kinds*.  Media is
        identified purely by MIME prefix — there is no media message type.

        Args:
            peer: CapAuth identity URI (or short name) to filter by.
            kinds: Media kinds to include.  Each maps to a MIME prefix:
                ``"image"`` -> ``"image/"``, ``"video"`` -> ``"video/"``.
                Any other kind is treated as ``"<kind>/"``.
            limit: Maximum number of media entries to return.

        Returns:
            list[dict]: One dict per matching attachment, newest-first, with
            keys ``message_id``, ``transfer_id``, ``filename``, ``mime_type``,
            ``size``, ``thumbnail_id``, ``direction``, ``timestamp`` (ISO-8601
            string) and ``sender``.
        """
        prefixes = tuple(f"{kind}/" for kind in kinds)
        # Load a generous window of messages; one message can carry several
        # attachments, so over-fetch then cap the flattened media list.
        messages = self.load(peer=peer, limit=max(limit * 4, limit))

        results: list[dict] = []
        for msg in messages:
            for ref in msg.attachments:
                if not ref.mime_type.startswith(prefixes):
                    continue
                results.append(
                    {
                        "message_id": msg.id,
                        "transfer_id": ref.transfer_id,
                        "filename": ref.filename,
                        "mime_type": ref.mime_type,
                        "size": ref.size,
                        "thumbnail_id": ref.thumbnail_id,
                        "direction": ref.direction,
                        "timestamp": msg.timestamp.isoformat(),
                        "sender": msg.sender,
                    }
                )
                if len(results) >= limit:
                    return results
        return results

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
            store_path = str(_skchat_home() / "memory")

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
            "content_type": str(message.content_type),
            "thread_id": message.thread_id,
            "reply_to_id": message.reply_to_id,
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
        return [self._memory_to_chat_dict(m) for m in memories if self.MESSAGE_TAG in m.tags]

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
            a_involved = meta.get("sender") in (participant_a, participant_b) and meta.get(
                "recipient"
            ) in (participant_a, participant_b)
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

    # ------------------------------------------------------------------
    # Convenience API (thin wrappers used by daemon / tests)
    # ------------------------------------------------------------------

    def add_message(self, sender: str, recipient: str, content: str) -> ChatMessage:
        """Create a ChatMessage and persist it to the JSONL history.

        Convenience wrapper around :meth:`save` that constructs the
        :class:`ChatMessage` from scalar arguments.

        Args:
            sender: CapAuth identity URI (or short name) of the sender.
            recipient: CapAuth identity URI (or short name) of the recipient.
            content: Plaintext message body.

        Returns:
            The created and saved :class:`ChatMessage`.
        """
        msg = ChatMessage(sender=sender, recipient=recipient, content=content)
        self.save(msg)
        return msg

    def get_messages(
        self,
        peer: str,
        limit: int = 50,
        since: Optional[datetime] = None,
    ) -> list[dict]:
        """Return recent messages involving *peer* as sender or recipient.

        Wraps :meth:`load` and converts results to plain dicts for easy
        display / JSON serialisation.

        Args:
            peer: CapAuth identity URI (or short name) to filter by.
            limit: Maximum number of messages to return (newest first).
            since: Optional lower bound on message timestamp.

        Returns:
            list[dict]: Each dict contains ``id``, ``sender``, ``recipient``,
            ``content``, and ``timestamp`` (ISO-8601 string).
        """
        messages = self.load(since=since, peer=peer, limit=limit)
        return [
            {
                "id": m.id,
                "sender": m.sender,
                "recipient": m.recipient,
                "content": m.content,
                "timestamp": m.timestamp.isoformat(),
            }
            for m in messages
        ]

    def get_thread(self, thread_id: str, limit: int = 50) -> list[ChatMessage]:
        """Return the most recent messages in *thread_id*, sorted oldest-first.

        Scans the on-disk JSONL files (no SKMemory required) and filters by
        ``message.thread_id == thread_id``.  Day-files (and lines within a
        file) are walked newest-first so that, once a thread has more than
        ``limit`` messages, the *most recent* ``limit`` are kept rather than
        the earliest ones. Results are then sorted by timestamp ascending so
        callers receive a natural conversation order.

        Args:
            thread_id: Thread identifier to filter on.
            limit: Maximum messages to return.

        Returns:
            list[ChatMessage]: The most recent ``limit`` messages, sorted by
            timestamp ascending.
        """
        files = sorted(self._history_dir.glob("*.jsonl"), reverse=True)  # newest date first
        results: list[ChatMessage] = []
        for path in files:
            if len(results) >= limit:
                break
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for raw in reversed(lines):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = ChatMessage.model_validate_json(raw)
                except Exception as e:
                    logger.warning("history.py: %s", e)
                    continue
                if msg.thread_id == thread_id:
                    results.append(msg)
                    if len(results) >= limit:
                        break
        results.sort(key=lambda m: m.timestamp)
        return results

    def get_thread_meta(self, thread_id: str) -> Optional[dict]:
        """Retrieve a specific thread's full metadata by ID from SKMemory.

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

    def get_messages_since(
        self,
        minutes: int,
        limit: int = 200,
        recipient: Optional[str] = None,
    ) -> list[dict]:
        """Retrieve messages stored within the last N minutes.

        Fetches all recent messages from the store and filters by the
        ``created_at`` timestamp so only messages newer than
        ``now - minutes`` are returned.

        Args:
            minutes: Look-back window in minutes.  Pass ``0`` (or any
                non-positive value) to return all messages regardless of age.
            limit: Upper bound on messages fetched before filtering.
            recipient: If given, only return messages where the stored
                ``recipient`` metadata field matches this value exactly.

        Returns:
            list[dict]: Message dicts sorted oldest-first so they read
                chronologically in a live inbox display.
        """
        if self._store is None:
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes) if minutes > 0 else None
        all_memories = self._store.list_memories(
            tags=[self.MESSAGE_TAG],
            limit=limit,
        )
        results: list[dict] = []
        for m in all_memories:
            # Apply time filter when a cutoff is set
            if cutoff is not None:
                ts = m.created_at
                if ts is None:
                    continue
                # Normalise to aware datetime for comparison
                if isinstance(ts, str):
                    try:
                        ts = datetime.fromisoformat(ts)
                    except ValueError:
                        continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    continue

            # Apply recipient filter
            if recipient is not None and m.metadata.get("recipient") != recipient:
                continue

            results.append(self._memory_to_chat_dict(m))

        results.sort(key=lambda d: d.get("timestamp", ""))
        return results

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
            "reply_to_id": memory.metadata.get("reply_to_id"),
            "delivery_status": memory.metadata.get("delivery_status"),
            "timestamp": memory.created_at,
            "tags": memory.tags,
        }
