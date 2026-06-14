import pytest

from skchat.spaces.lanes import LaneDispatcher, LaneStore


def _disp(tmp_path):
    return LaneDispatcher(store=LaneStore(db_path=tmp_path / "l.db"))


def test_dispatch_log_lane_appends(tmp_path):
    d = _disp(tmp_path)
    d.dispatch("s1", {"lane": "chat", "text": "hi"})
    d.dispatch("s1", {"lane": "chat", "text": "yo"})
    assert [e["text"] for e in d.store.replay("s1", "chat")] == ["hi", "yo"]


def test_dispatch_snapshot_lane_replaces(tmp_path):
    d = _disp(tmp_path)
    d.dispatch("s1", {"lane": "whiteboard", "elements": [1]})
    d.dispatch("s1", {"lane": "whiteboard", "elements": [1, 2]})
    assert d.store.replay("s1", "whiteboard") == [{"lane": "whiteboard", "elements": [1, 2]}]


def test_unknown_lane_rejected(tmp_path):
    with pytest.raises(ValueError, match="lane"):
        _disp(tmp_path).dispatch("s1", {"lane": "bogus", "x": 1})


def test_missing_lane_field_rejected(tmp_path):
    with pytest.raises(ValueError, match="lane"):
        _disp(tmp_path).dispatch("s1", {"text": "no lane key"})


def test_non_dict_payload_rejected(tmp_path):
    with pytest.raises(ValueError):
        _disp(tmp_path).dispatch("s1", ["not", "a", "dict"])
