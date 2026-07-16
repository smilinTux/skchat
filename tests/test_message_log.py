"""Tests for ``skchat.message_log`` — the single-writer authoritative log.

The MessageLog assigns a monotonic per-conversation ``seq`` and an immutable
server ``message_id`` inside one transaction (the single writer), and is
idempotent on ``client_dedup_key`` (or a supplied ``message_id``): a repeat
returns the existing row flagged ``deduped=True`` and never burns a second seq.
"""

from __future__ import annotations

import threading

from skchat.message_log import MessageLog


def test_append_assigns_monotonic_seq(tmp_path):
    log = MessageLog(str(tmp_path / "m.db"))
    a = log.append("c1", sender="s", recipient="r", content="one")
    b = log.append("c1", sender="s", recipient="r", content="two")
    assert a["seq"] == 1 and b["seq"] == 2
    assert a["message_id"] and a["message_id"] != b["message_id"]
    assert a["deduped"] is False and b["deduped"] is False


def test_seq_is_per_conversation(tmp_path):
    log = MessageLog(str(tmp_path / "m.db"))
    assert log.append("c1", sender="s", recipient="r", content="x")["seq"] == 1
    assert log.append("c2", sender="s", recipient="r", content="y")["seq"] == 1


def test_client_dedup_key_is_idempotent(tmp_path):
    log = MessageLog(str(tmp_path / "m.db"))
    a = log.append("c1", client_dedup_key="k1", sender="s", recipient="r", content="hi")
    b = log.append("c1", client_dedup_key="k1", sender="s", recipient="r", content="hi again")
    assert b["seq"] == a["seq"] and b["deduped"] is True  # no second seq
    assert b["message_id"] == a["message_id"]
    assert log.latest_seq("c1") == 1


def test_message_id_is_idempotent(tmp_path):
    log = MessageLog(str(tmp_path / "m.db"))
    a = log.append("c1", message_id="fixed-id", sender="s", recipient="r", content="hi")
    b = log.append("c1", message_id="fixed-id", sender="s", recipient="r", content="hi again")
    assert b["seq"] == a["seq"] and b["deduped"] is True
    assert log.latest_seq("c1") == 1


def test_read_returns_ordered_since_seq(tmp_path):
    log = MessageLog(str(tmp_path / "m.db"))
    for i in range(3):
        log.append("c1", sender="s", recipient="r", content=str(i))
    rows = log.read("c1", since_seq=1)
    assert [r["seq"] for r in rows] == [2, 3]
    assert [r["content"] for r in rows] == ["1", "2"]


def test_read_is_scoped_to_conversation(tmp_path):
    log = MessageLog(str(tmp_path / "m.db"))
    log.append("c1", sender="s", recipient="r", content="a")
    log.append("c2", sender="s", recipient="r", content="b")
    rows = log.read("c1")
    assert [r["content"] for r in rows] == ["a"]


def test_latest_seq_empty_conversation_is_zero(tmp_path):
    log = MessageLog(str(tmp_path / "m.db"))
    assert log.latest_seq("nope") == 0


def test_creates_parent_dir_before_connect(tmp_path):
    # Parent dir does not exist yet — the log must mkdir it (LaneStore-style).
    db = tmp_path / "nested" / "deeper" / "m.db"
    log = MessageLog(str(db))
    log.append("c1", sender="s", recipient="r", content="x")
    assert db.exists()


def test_concurrent_appends_get_unique_contiguous_seqs(tmp_path):
    """Single-writer invariant: N threads appending to ONE conversation get
    seqs exactly 1..N with no gaps and no duplicates."""
    log = MessageLog(str(tmp_path / "m.db"))
    n = 40
    barrier = threading.Barrier(n)
    results: list[int] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait()  # maximize contention: everyone appends at once
        row = log.append("c1", sender="s", recipient="r", content=f"m{i}")
        with lock:
            results.append(row["seq"])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == list(range(1, n + 1))  # no gaps, no dupes
    assert log.latest_seq("c1") == n


# ── Task 1: canonical conversation ids + idempotent record() ─────────────────

from datetime import datetime, timezone  # noqa: E402

from skchat.message_log import conversation_id_for, dedup_key_for  # noqa: E402
from skchat.models import ChatMessage  # noqa: E402


def test_conversation_id_group_and_dm():
    g = ChatMessage(sender="lumina", recipient="group:abc", content="hi")
    assert conversation_id_for(g) == "group:abc"
    # a member copy that names its group thread still maps to the group
    m = ChatMessage(sender="lumina", recipient="chef", content="hi", thread_id="group:abc")
    assert conversation_id_for(m) == "group:abc"


def test_dm_conversation_id_is_direction_independent():
    a2b = ChatMessage(sender="alice", recipient="bob", content="hi")
    b2a = ChatMessage(sender="bob", recipient="alice", content="hi")
    assert conversation_id_for(a2b) == conversation_id_for(b2a) == "dm:alice|bob"


def test_record_is_idempotent_by_id_and_dedup(tmp_path):
    log = MessageLog(str(tmp_path / "m.db"))
    ts = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    msg = ChatMessage(sender="alice", recipient="bob", content="hello", timestamp=ts)
    first = log.record(msg)
    second = log.record(msg)  # same object -> same id -> dedup
    assert first["deduped"] is False and second["deduped"] is True
    assert first["seq"] == second["seq"]
    # exactly one row in the conversation
    assert len(log.read(conversation_id_for(msg))) == 1


def test_record_collapses_fanout_copies_by_dedup_key(tmp_path):
    # the 1+N fan-out copies share sender+conversation+content+second but have
    # DIFFERENT ids; dedup_key_for collapses them to one row on re-record.
    log = MessageLog(str(tmp_path / "m.db"))
    ts = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    canonical = ChatMessage(sender="lumina", recipient="group:g1", content="team", timestamp=ts)
    # a "member copy" of the same logical message: different id, member recipient,
    # but the group thread -> same conversation + same dedup key.
    member_copy = ChatMessage(
        sender="lumina", recipient="chef", content="team", timestamp=ts, thread_id="group:g1"
    )
    assert canonical.id != member_copy.id
    assert dedup_key_for(canonical) == dedup_key_for(member_copy)
    log.record(canonical)
    log.record(member_copy)
    assert len(log.read("group:g1")) == 1


def test_record_stores_full_payload_roundtrip(tmp_path):
    from skchat.message_log import log_row_to_message
    log = MessageLog(str(tmp_path / "m.db"))
    msg = ChatMessage(sender="a", recipient="b", content="hi", reply_to_id="r1",
                      metadata={"k": "v", "attachments": [{"name": "f.png"}]})
    log.record(msg)
    row = log.read(conversation_id_for(msg))[0]
    back = log_row_to_message(row)
    assert back.id == msg.id and back.reply_to_id == "r1"
    assert back.metadata.get("k") == "v"
    assert back.metadata.get("attachments") == [{"name": "f.png"}]


def test_recent_and_conversations(tmp_path):
    log = MessageLog(str(tmp_path / "m.db"))
    from datetime import datetime, timezone
    for i, (conv, sender, content) in enumerate([
        ("dm:a|b", "a", "one"), ("dm:a|b", "b", "two"), ("group:g", "a", "g1"),
    ]):
        log.append(conv, sender=sender, recipient="x", content=content,
                   client_dedup_key=f"k{i}")
    # recent: newest-first across all conversations
    rec = log.recent(limit=10)
    assert len(rec) == 3 and rec[0]["content"] == "g1"
    # conversations: latest event per conversation
    convs = log.conversations()
    by_conv = {c["conversation_id"]: c["content"] for c in convs}
    assert by_conv == {"dm:a|b": "two", "group:g": "g1"}
