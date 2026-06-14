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
    assert out == [{"lane": "whiteboard", "elements": [1, 2]}]   # only latest


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
    assert [e["i"] for e in out] == [7, 8, 9]   # most-recent 3, chronological


def test_empty_replay_is_empty_list(tmp_path):
    assert _store(tmp_path).replay("nope", "chat") == []
