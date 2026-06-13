import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.spaces.consent import ConsentLedger
from skchat.spaces.registry import SpaceRegistry
from skchat.spaces.routes import register_spaces_routes


class FakeRecorder:
    def __init__(self):
        self.started, self.stopped = [], []

    async def start(self, room, filepath):
        self.started.append((room, filepath))
        return "EG_xyz"

    async def stop(self, egress_id):
        self.stopped.append(egress_id)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "s")
    app = FastAPI()
    reg = SpaceRegistry(path=tmp_path / "s.json")
    led = ConsentLedger(path=tmp_path / "c.json")
    rec = FakeRecorder()
    register_spaces_routes(app, registry=reg, consent=led, recorder=rec)
    c = TestClient(app)
    sid = c.post("/spaces/create", json={
        "host_fqid": "lumina@chef.skworld", "title": "T", "slug": "s"}).json()["space_id"]
    return c, sid, rec, led


def test_record_blocked_until_speakers_consent(setup):
    c, sid, rec, led = setup
    r = c.post(f"/spaces/{sid}/record/start", json={
        "requester": "lumina@chef.skworld", "speakers": ["alice@x.y"]})
    assert r.status_code == 409
    assert r.json()["missing_consent"] == ["alice@x.y"]
    assert rec.started == []                       # not started


def test_record_starts_after_consent(setup):
    c, sid, rec, led = setup
    c.post(f"/spaces/{sid}/consent", json={"identity": "alice@x.y"})
    r = c.post(f"/spaces/{sid}/record/start", json={
        "requester": "lumina@chef.skworld", "speakers": ["alice@x.y"]})
    assert r.status_code == 200
    assert len(rec.started) == 1
    # REC reflected in the live listing
    live = c.get("/spaces").json()["spaces"]
    assert next(s for s in live if s["space_id"] == sid)["recording"] is True


def test_non_host_cannot_record(setup):
    c, sid, rec, led = setup
    r = c.post(f"/spaces/{sid}/record/start", json={
        "requester": "rando@x.y", "speakers": []})
    assert r.status_code == 403


def test_stop_recording(setup):
    c, sid, rec, led = setup
    c.post(f"/spaces/{sid}/consent", json={"identity": "alice@x.y"})
    c.post(f"/spaces/{sid}/record/start", json={
        "requester": "lumina@chef.skworld", "speakers": ["alice@x.y"]})
    r = c.post(f"/spaces/{sid}/record/stop", json={"requester": "lumina@chef.skworld"})
    assert r.status_code == 200
    assert rec.stopped == ["EG_xyz"]
    live = c.get("/spaces").json()["spaces"]
    assert next(s for s in live if s["space_id"] == sid)["recording"] is False
