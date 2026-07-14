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
