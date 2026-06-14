import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.spaces.lanes import LaneStore
from skchat.spaces.routes import register_spaces_routes


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "s")
    app = FastAPI()
    register_spaces_routes(app, lane_store=LaneStore(db_path=tmp_path / "l.db"))
    return TestClient(app)


def test_post_event_then_replay_log_lane(client):
    r = client.post("/spaces/s1/lanes/event", json={"lane": "chat", "text": "hello"})
    assert r.status_code == 200
    out = client.get("/spaces/s1/lanes/chat/state").json()
    assert out["events"][-1]["text"] == "hello"


def test_post_snapshot_then_replay_returns_latest(client):
    client.post("/spaces/s1/lanes/event", json={"lane": "whiteboard", "elements": [1]})
    client.post("/spaces/s1/lanes/event", json={"lane": "whiteboard", "elements": [1, 2]})
    out = client.get("/spaces/s1/lanes/whiteboard/state").json()
    assert out["events"] == [{"lane": "whiteboard", "elements": [1, 2]}]


def test_unknown_lane_is_400(client):
    r = client.post("/spaces/s1/lanes/event", json={"lane": "bogus"})
    assert r.status_code == 400


def test_replay_unknown_lane_is_400(client):
    assert client.get("/spaces/s1/lanes/bogus/state").status_code == 400


def test_empty_state_ok(client):
    out = client.get("/spaces/s1/lanes/chat/state").json()
    assert out["events"] == []


# ---------------------------------------------------------------------------
# QA Area 2 — additional lane-route coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lane", ["chat", "watch", "doc", "term"])
def test_log_lanes_post_and_replay_via_routes(client, lane):
    client.post("/spaces/s1/lanes/event", json={"lane": lane, "i": 1})
    client.post("/spaces/s1/lanes/event", json={"lane": lane, "i": 2})
    out = client.get(f"/spaces/s1/lanes/{lane}/state").json()
    assert [e["i"] for e in out["events"]] == [1, 2]


def test_event_route_scopes_state_per_space(client):
    client.post("/spaces/s1/lanes/event", json={"lane": "chat", "text": "a"})
    client.post("/spaces/s2/lanes/event", json={"lane": "chat", "text": "b"})
    assert [e["text"] for e in client.get("/spaces/s1/lanes/chat/state").json()["events"]] == ["a"]
    assert [e["text"] for e in client.get("/spaces/s2/lanes/chat/state").json()["events"]] == ["b"]


def test_snapshot_route_scoped_per_space(client):
    client.post("/spaces/s1/lanes/event", json={"lane": "whiteboard", "rev": "a"})
    client.post("/spaces/s2/lanes/event", json={"lane": "whiteboard", "rev": "b"})
    assert client.get("/spaces/s1/lanes/whiteboard/state").json()["events"] == [
        {"lane": "whiteboard", "rev": "a"}
    ]


def test_missing_lane_field_in_event_is_400(client):
    r = client.post("/spaces/s1/lanes/event", json={"text": "no lane"})
    assert r.status_code == 400


def test_term_event_is_persisted_but_not_executed(client):
    """Posting a term run-request via the generic event route persists it
    (replayable) and never executes it."""
    r = client.post(
        "/spaces/s1/lanes/event",
        json={"lane": "term", "action": "run", "cmd": "ls"},
    )
    assert r.status_code == 200
    out = client.get("/spaces/s1/lanes/term/state").json()
    assert out["events"][-1]["cmd"] == "ls"
    # No "output"/"exit" events were synthesised — only the raw request is stored.
    assert all(e.get("action") != "exit" for e in out["events"])
