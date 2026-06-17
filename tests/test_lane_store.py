from skchat.spaces.lanes import LaneStore


def _store(tmp_path):
    return LaneStore(db_path=tmp_path / "lanes.db")


def test_log_lane_appends_and_replays_in_order(tmp_path):
    s = _store(tmp_path)
    s.append("space-1", "chat", {"lane": "chat", "from": "a", "text": "hi", "ts": 1})
    s.append("space-1", "chat", {"lane": "chat", "from": "b", "text": "yo", "ts": 2})
    out = s.replay("space-1", "chat")
    assert [e["text"] for e in out] == ["hi", "yo"]


def test_snapshot_lane_keeps_only_latest(tmp_path):
    s = _store(tmp_path)
    s.snapshot("space-1", "whiteboard", {"lane": "whiteboard", "elements": [1]})
    s.snapshot("space-1", "whiteboard", {"lane": "whiteboard", "elements": [1, 2]})
    out = s.replay("space-1", "whiteboard")
    assert out == [{"lane": "whiteboard", "elements": [1, 2]}]  # only latest


def test_replay_scoped_per_space_and_lane(tmp_path):
    s = _store(tmp_path)
    s.append("space-1", "chat", {"text": "one"})
    s.append("space-2", "chat", {"text": "two"})
    assert [e["text"] for e in s.replay("space-1", "chat")] == ["one"]


def test_log_replay_respects_limit(tmp_path):
    s = _store(tmp_path)
    for i in range(10):
        s.append("space-1", "watch", {"i": i})
    out = s.replay("space-1", "watch", limit=3)
    assert [e["i"] for e in out] == [7, 8, 9]  # most-recent 3, chronological


def test_empty_replay_is_empty_list(tmp_path):
    assert _store(tmp_path).replay("nope", "chat") == []


# ---------------------------------------------------------------------------
# QA Area 2 — additional lane-store coverage
# ---------------------------------------------------------------------------


def test_snapshot_replay_returns_latest_when_re_snapshotting_many_times(tmp_path):
    """snapshot() deletes the prior snapshot every time → exactly one row, latest."""
    s = _store(tmp_path)
    for n in range(5):
        s.snapshot("space-1", "whiteboard", {"lane": "whiteboard", "rev": n})
    out = s.replay("space-1", "whiteboard")
    assert out == [{"lane": "whiteboard", "rev": 4}]  # only the final revision


def test_snapshot_lane_does_not_accumulate_rows(tmp_path):
    """Re-snapshotting must not grow the table (delete-then-insert)."""
    s = _store(tmp_path)
    for n in range(20):
        s.snapshot("space-1", "whiteboard", {"rev": n})
    # replay never returns more than the single latest snapshot.
    assert len(s.replay("space-1", "whiteboard")) == 1


def test_log_replay_preserves_insertion_order_not_timestamp_ties(tmp_path):
    """Ordering is by autoincrement id, so rapid appends with equal ts stay ordered."""
    s = _store(tmp_path)
    for i in range(50):
        s.append("space-1", "chat", {"seq": i})
    out = s.replay("space-1", "chat")
    assert [e["seq"] for e in out] == list(range(50))


def test_replay_scoped_per_lane_within_a_space(tmp_path):
    """Two lanes in the SAME space must not bleed into each other."""
    s = _store(tmp_path)
    s.append("space-1", "chat", {"text": "chat-msg"})
    s.append("space-1", "watch", {"text": "watch-evt"})
    assert [e["text"] for e in s.replay("space-1", "chat")] == ["chat-msg"]
    assert [e["text"] for e in s.replay("space-1", "watch")] == ["watch-evt"]


def test_snapshot_scoped_per_space(tmp_path):
    """Snapshotting one space must not clobber another space's snapshot."""
    s = _store(tmp_path)
    s.snapshot("space-1", "whiteboard", {"rev": "a"})
    s.snapshot("space-2", "whiteboard", {"rev": "b"})
    assert s.replay("space-1", "whiteboard") == [{"rev": "a"}]
    assert s.replay("space-2", "whiteboard") == [{"rev": "b"}]


def test_replay_limit_zero_returns_empty(tmp_path):
    """A limit of 0 returns no rows (LIMIT 0)."""
    s = _store(tmp_path)
    s.append("space-1", "chat", {"i": 1})
    assert s.replay("space-1", "chat", limit=0) == []


def test_replay_limit_larger_than_count_returns_all(tmp_path):
    s = _store(tmp_path)
    for i in range(3):
        s.append("space-1", "chat", {"i": i})
    out = s.replay("space-1", "chat", limit=1000)
    assert [e["i"] for e in out] == [0, 1, 2]


def test_store_persists_across_reopen(tmp_path):
    """A fresh LaneStore on the same db sees previously-appended events."""
    db = tmp_path / "lanes.db"
    s1 = LaneStore(db_path=db)
    s1.append("space-1", "doc", {"text": "persisted"})
    s2 = LaneStore(db_path=db)
    assert [e["text"] for e in s2.replay("space-1", "doc")] == ["persisted"]


def test_nested_payload_survives_json_roundtrip(tmp_path):
    """Arbitrary nested JSON-serialisable payloads round-trip intact."""
    s = _store(tmp_path)
    payload = {"lane": "doc", "ops": [{"insert": "x"}, {"retain": 3}], "n": 1.5}
    s.append("space-1", "doc", payload)
    assert s.replay("space-1", "doc") == [payload]
