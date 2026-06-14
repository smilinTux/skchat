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
