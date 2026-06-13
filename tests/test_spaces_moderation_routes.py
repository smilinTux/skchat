import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.spaces.registry import SpaceRegistry
from skchat.spaces.routes import register_spaces_routes


class FakeModerator:
    def __init__(self):
        self.calls = []

    async def stage_action(self, room, identity, action):
        self.calls.append(("stage", room, identity, action))
        return action == "invite"  # pretend invite reaches stage

    async def kick(self, room, identity):
        self.calls.append(("kick", room, identity))

    async def mute(self, room, identity, track_sid):
        self.calls.append(("mute", room, identity, track_sid))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "s")
    app = FastAPI()
    reg = SpaceRegistry(path=tmp_path / "s.json")
    mod = FakeModerator()
    register_spaces_routes(app, registry=reg, moderator=mod)
    c = TestClient(app)
    sid = c.post("/spaces/create", json={
        "host_fqid": "lumina@chef.skworld", "title": "T", "slug": "s"}).json()["space_id"]
    return c, sid, mod


def test_listener_can_raise_own_hand(setup):
    c, sid, mod = setup
    r = c.post(f"/spaces/{sid}/raise-hand", json={"identity": "alice@x.y"})
    assert r.status_code == 200
    assert ("stage", sid, "alice@x.y", "raise_hand") in mod.calls


def test_host_can_invite(setup):
    c, sid, mod = setup
    r = c.post(f"/spaces/{sid}/invite", json={
        "requester": "lumina@chef.skworld", "identity": "alice@x.y"})
    assert r.status_code == 200
    assert r.json()["on_stage"] is True
    assert ("stage", sid, "alice@x.y", "invite") in mod.calls


def test_non_host_cannot_invite(setup):
    c, sid, mod = setup
    r = c.post(f"/spaces/{sid}/invite", json={
        "requester": "rando@x.y", "identity": "alice@x.y"})
    assert r.status_code == 403
    assert all(call[0] != "stage" or call[3] != "invite" for call in mod.calls)


def test_non_host_cannot_kick(setup):
    c, sid, mod = setup
    r = c.post(f"/spaces/{sid}/kick", json={
        "requester": "rando@x.y", "identity": "alice@x.y"})
    assert r.status_code == 403


def test_host_can_kick_and_mute(setup):
    c, sid, mod = setup
    assert c.post(f"/spaces/{sid}/kick", json={
        "requester": "lumina@chef.skworld", "identity": "troll@x.y"}).status_code == 200
    assert c.post(f"/spaces/{sid}/mute", json={
        "requester": "lumina@chef.skworld", "identity": "loud@x.y",
        "track_sid": "TR_1"}).status_code == 200
    assert ("kick", sid, "troll@x.y") in mod.calls
    assert ("mute", sid, "loud@x.y", "TR_1") in mod.calls


def test_self_can_remove_from_stage(setup):
    c, sid, mod = setup
    r = c.post(f"/spaces/{sid}/remove-from-stage", json={
        "requester": "alice@x.y", "identity": "alice@x.y"})
    assert r.status_code == 200  # self-removal allowed
