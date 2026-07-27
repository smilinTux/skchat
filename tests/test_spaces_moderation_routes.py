import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skchat.spaces.consent import ConsentLedger
from skchat.spaces.registry import SpaceRegistry
from skchat.spaces.routes import register_spaces_routes
from skchat.spaces.space import Space


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

    async def set_sharing(self, room, identity, allow):
        self.calls.append(("set_sharing", room, identity, allow))
        return allow


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "s")
    app = FastAPI()
    reg = SpaceRegistry(path=tmp_path / "s.json")
    led = ConsentLedger(path=tmp_path / "c.json")
    mod = FakeModerator()
    register_spaces_routes(app, registry=reg, moderator=mod, consent=led)
    c = TestClient(app)
    sid = c.post(
        "/spaces/create", json={"host_fqid": "lumina@chef.skworld", "title": "T", "slug": "s"}
    ).json()["space_id"]
    return c, sid, mod, reg, led


def test_listener_can_raise_own_hand(setup):
    c, sid, mod, reg, led = setup
    r = c.post(f"/spaces/{sid}/raise-hand", json={"identity": "alice@x.y"})
    assert r.status_code == 200
    assert ("stage", sid, "alice@x.y", "raise_hand") in mod.calls


def test_host_can_invite(setup):
    c, sid, mod, reg, led = setup
    r = c.post(
        f"/spaces/{sid}/invite", json={"requester": "lumina@chef.skworld", "identity": "alice@x.y"}
    )
    assert r.status_code == 200
    assert r.json()["on_stage"] is True
    assert ("stage", sid, "alice@x.y", "invite") in mod.calls


def test_invite_to_stage_adds_authoritative_speaker(setup):
    """I1: a successful invite records the speaker in the authoritative set, and
    the directory `${speakers} on stage` count reflects reg.speakers."""
    c, sid, mod, reg, led = setup
    c.post(
        f"/spaces/{sid}/invite", json={"requester": "lumina@chef.skworld", "identity": "alice@x.y"}
    )
    assert reg.get(sid).speakers == ["alice@x.y"]
    live = c.get("/spaces").json()["spaces"]
    assert next(s for s in live if s["space_id"] == sid)["speakers"] == ["alice@x.y"]


def test_remove_from_stage_drops_authoritative_speaker(setup):
    c, sid, mod, reg, led = setup
    reg.add_speaker(sid, "alice@x.y")
    r = c.post(
        f"/spaces/{sid}/remove-from-stage",
        json={"requester": "lumina@chef.skworld", "identity": "alice@x.y"},
    )
    assert r.status_code == 200
    assert reg.get(sid).speakers == []


def test_non_host_cannot_invite(setup):
    c, sid, mod, reg, led = setup
    r = c.post(f"/spaces/{sid}/invite", json={"requester": "rando@x.y", "identity": "alice@x.y"})
    assert r.status_code == 403
    assert all(call[0] != "stage" or call[3] != "invite" for call in mod.calls)


def test_non_host_cannot_kick(setup):
    c, sid, mod, reg, led = setup
    r = c.post(f"/spaces/{sid}/kick", json={"requester": "rando@x.y", "identity": "alice@x.y"})
    assert r.status_code == 403


def test_host_can_kick_and_mute(setup):
    c, sid, mod, reg, led = setup
    assert (
        c.post(
            f"/spaces/{sid}/kick",
            json={"requester": "lumina@chef.skworld", "identity": "troll@x.y"},
        ).status_code
        == 200
    )
    assert (
        c.post(
            f"/spaces/{sid}/mute",
            json={"requester": "lumina@chef.skworld", "identity": "loud@x.y", "track_sid": "TR_1"},
        ).status_code
        == 200
    )
    assert ("kick", sid, "troll@x.y") in mod.calls
    assert ("mute", sid, "loud@x.y", "TR_1") in mod.calls


def test_non_host_cannot_set_sharing(setup):
    c, sid, mod, reg, led = setup
    r = c.post(
        f"/spaces/{sid}/set-sharing",
        json={"requester": "rando@x.y", "identity": "alice@x.y", "allow": False},
    )
    assert r.status_code == 403
    assert all(call[0] != "set_sharing" for call in mod.calls)


def test_host_can_disable_sharing(setup):
    c, sid, mod, reg, led = setup
    r = c.post(
        f"/spaces/{sid}/set-sharing",
        json={"requester": "lumina@chef.skworld", "identity": "alice@x.y", "allow": False},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "sharing": False}
    assert ("set_sharing", sid, "alice@x.y", False) in mod.calls


def test_host_can_re_allow_sharing(setup):
    c, sid, mod, reg, led = setup
    r = c.post(
        f"/spaces/{sid}/set-sharing",
        json={"requester": "lumina@chef.skworld", "identity": "alice@x.y", "allow": True},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "sharing": True}
    assert ("set_sharing", sid, "alice@x.y", True) in mod.calls


def test_set_sharing_unknown_space_404s(setup):
    c, sid, mod, reg, led = setup
    r = c.post(
        "/spaces/nope-space-000000000/set-sharing",
        json={"requester": "lumina@chef.skworld", "identity": "alice@x.y", "allow": False},
    )
    assert r.status_code == 404


def test_self_can_remove_from_stage(setup):
    c, sid, mod, reg, led = setup
    r = c.post(
        f"/spaces/{sid}/remove-from-stage",
        json={"requester": "alice@x.y", "identity": "alice@x.y"},
    )
    assert r.status_code == 200  # self-removal allowed


def test_blank_host_cannot_be_impersonated(setup, tmp_path):
    c, sid, mod, reg, led = setup
    # simulate a loaded space with an empty host_fqid by ending+recreating via the
    # registry isn't exposed here; instead assert an empty requester is rejected
    # even though strip() makes it "" (the non-empty host guard handles the rest).
    r = c.post(f"/spaces/{sid}/invite", json={"requester": "", "identity": "x@y.z"})
    assert r.status_code == 403


def test_blank_host_fqid_guard_first_clause(tmp_path, monkeypatch):
    """M4: a Space with host_fqid="" must reject a NON-empty requester on /invite,
    exercising the `not space.host_fqid.strip()` first clause of _require_host."""
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("SKCHAT_LIVEKIT_API_SECRET", "s")
    app = FastAPI()
    reg = SpaceRegistry(path=tmp_path / "s.json")
    reg.add(Space(space_id="space-blankhost000000", host_fqid="", title="T", slug="s"))
    register_spaces_routes(app, registry=reg, moderator=FakeModerator())
    c = TestClient(app)
    r = c.post(
        "/spaces/space-blankhost000000/invite",
        json={"requester": "someone@x.y", "identity": "alice@x.y"},
    )
    assert r.status_code == 403


def test_promote_while_recording_blocks_unconsented(setup):
    """I3: while recording is active, promoting a non-consenting identity is
    reverted (remove stage_action) and 409'd; they are NOT added to speakers."""
    c, sid, mod, reg, led = setup
    reg.set_recording(sid, True, "EG_x")
    r = c.post(
        f"/spaces/{sid}/invite", json={"requester": "lumina@chef.skworld", "identity": "alice@x.y"}
    )
    assert r.status_code == 409
    assert "consent" in r.json()["detail"].lower()
    assert reg.get(sid).speakers == []
    # the promotion was reverted via a remove stage_action
    assert ("stage", sid, "alice@x.y", "remove") in mod.calls


def test_promote_while_recording_allows_consented(setup):
    """I3: a consenting identity CAN be promoted while recording is active."""
    c, sid, mod, reg, led = setup
    c.post(f"/spaces/{sid}/consent", json={"identity": "alice@x.y"})
    reg.set_recording(sid, True, "EG_x")
    r = c.post(
        f"/spaces/{sid}/invite", json={"requester": "lumina@chef.skworld", "identity": "alice@x.y"}
    )
    assert r.status_code == 200
    assert reg.get(sid).speakers == ["alice@x.y"]
