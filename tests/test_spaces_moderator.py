import json

import pytest

from skchat.spaces.moderation import Moderator


class FakeParticipant:
    def __init__(self, metadata=""):
        self.metadata = metadata


class FakeRoomService:
    def __init__(self):
        self.updates = []
        self.removed = []
        self.muted = []
        self._participants = {}

    def set_participant(self, identity, metadata):
        self._participants[identity] = FakeParticipant(metadata)

    async def get_participant(self, req):
        return self._participants.get(req.identity, FakeParticipant(""))

    async def update_participant(self, req):
        self.updates.append(req)
        # reflect new metadata so subsequent reads see it
        self._participants[req.identity] = FakeParticipant(req.metadata or "")

    async def remove_participant(self, req):
        self.removed.append(req.identity)

    async def mute_published_track(self, req):
        self.muted.append((req.identity, req.track_sid, req.muted))


@pytest.fixture
def fake():
    return FakeRoomService()


@pytest.fixture
def mod(fake):
    return Moderator("ws://test:7880", "k", "s", _room_service=fake)


@pytest.mark.asyncio
async def test_raise_hand_sets_metadata_but_not_publish(mod, fake):
    cp = await mod.stage_action("space-x", "alice", "raise_hand")
    assert cp is False
    assert len(fake.updates) == 1
    meta = json.loads(fake.updates[-1].metadata)
    assert meta["hand_raised"] is True
    # permission.can_publish must be False (no premature publish)
    assert fake.updates[-1].permission.can_publish is False


@pytest.mark.asyncio
async def test_invite_after_raise_promotes_to_publisher(mod, fake):
    fake.set_participant("alice", json.dumps({"hand_raised": True,
                                              "invited_to_stage": False}))
    cp = await mod.stage_action("space-x", "alice", "invite")
    assert cp is True
    assert fake.updates[-1].permission.can_publish is True


@pytest.mark.asyncio
async def test_remove_from_stage_demotes(mod, fake):
    fake.set_participant("alice", json.dumps({"hand_raised": True,
                                              "invited_to_stage": True}))
    cp = await mod.stage_action("space-x", "alice", "remove")
    assert cp is False
    assert fake.updates[-1].permission.can_publish is False


@pytest.mark.asyncio
async def test_kick_removes_participant(mod, fake):
    await mod.kick("space-x", "troll")
    assert fake.removed == ["troll"]


@pytest.mark.asyncio
async def test_mute_mutes_track(mod, fake):
    await mod.mute("space-x", "loud", "TR_abc")
    assert fake.muted == [("loud", "TR_abc", True)]
