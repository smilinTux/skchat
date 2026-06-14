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


# ---------------------------------------------------------------------------
# QA Area 2 — additional dispatcher coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lane", ["chat", "watch", "doc", "term"])
def test_all_log_lanes_route_to_append(tmp_path, lane):
    """The four log lanes all append (no snapshot-collapse)."""
    d = _disp(tmp_path)
    d.dispatch("s1", {"lane": lane, "n": 1})
    d.dispatch("s1", {"lane": lane, "n": 2})
    assert [e["n"] for e in d.store.replay("s1", lane)] == [1, 2]


def test_term_run_request_is_persisted_not_executed(tmp_path):
    """A term-lane run envelope is dispatched as a LOG append — the dispatcher
    never executes it (execution is the explicit term/run route's job)."""
    d = _disp(tmp_path)
    d.dispatch("s1", {"lane": "term", "action": "run", "cmd": "rm -rf /"})
    out = d.store.replay("s1", "term")
    assert out == [{"lane": "term", "action": "run", "cmd": "rm -rf /"}]


def test_whiteboard_is_the_only_snapshot_lane(tmp_path):
    """Only whiteboard collapses to latest; the others accumulate."""
    from skchat.spaces.lanes import LOG_LANES, SNAPSHOT_LANES

    assert SNAPSHOT_LANES == frozenset({"whiteboard"})
    assert LOG_LANES == frozenset({"chat", "watch", "doc", "term"})


def test_dispatch_preserves_full_envelope(tmp_path):
    """The whole envelope (not just a subset) is stored verbatim."""
    d = _disp(tmp_path)
    env = {"lane": "chat", "from": "a@x", "text": "hi", "ts": 9, "extra": [1, 2]}
    d.dispatch("s1", env)
    assert d.store.replay("s1", "chat") == [env]


def test_empty_string_lane_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="lane"):
        _disp(tmp_path).dispatch("s1", {"lane": "", "x": 1})


def test_none_lane_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="lane"):
        _disp(tmp_path).dispatch("s1", {"lane": None})
