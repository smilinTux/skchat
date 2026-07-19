import asyncio
import json

import pytest
from livekit import api

from skchat.spaces.moderation import Moderator


class FakeParticipant:
    def __init__(self, metadata=""):
        self.metadata = metadata


class FakeTrack:
    def __init__(self, sid, source, muted=False):
        self.sid = sid
        self.source = source
        self.muted = muted


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
    fake.set_participant("alice", json.dumps({"hand_raised": True, "invited_to_stage": False}))
    cp = await mod.stage_action("space-x", "alice", "invite")
    assert cp is True
    assert fake.updates[-1].permission.can_publish is True


@pytest.mark.asyncio
async def test_remove_from_stage_demotes(mod, fake):
    fake.set_participant("alice", json.dumps({"hand_raised": True, "invited_to_stage": True}))
    cp = await mod.stage_action("space-x", "alice", "remove")
    assert cp is False
    assert fake.updates[-1].permission.can_publish is False


@pytest.mark.asyncio
async def test_remove_force_mutes_published_mic_track(mod, fake):
    # backstop: a frozen/malicious client may ignore the can_publish revoke and
    # keep publishing, so "remove" must also force-mute any already-published
    # microphone track. A camera track must be left alone.
    fake.set_participant("alice", json.dumps({"hand_raised": True, "invited_to_stage": True}))
    fake._participants["alice"].tracks = [
        FakeTrack("TR_mic", api.TrackSource.MICROPHONE),
        FakeTrack("TR_cam", api.TrackSource.CAMERA),
    ]
    await mod.stage_action("space-x", "alice", "remove")
    assert fake.muted == [("alice", "TR_mic", True)]


@pytest.mark.asyncio
async def test_remove_with_no_published_track_is_noop(mod, fake):
    # best effort: no published track means no mute call and no error.
    fake.set_participant("alice", json.dumps({"hand_raised": True, "invited_to_stage": True}))
    await mod.stage_action("space-x", "alice", "remove")
    assert fake.muted == []


@pytest.mark.asyncio
async def test_non_remove_actions_do_not_force_mute(mod, fake):
    fake.set_participant("alice", json.dumps({"hand_raised": True, "invited_to_stage": False}))
    fake._participants["alice"].tracks = [FakeTrack("TR_mic", api.TrackSource.MICROPHONE)]
    await mod.stage_action("space-x", "alice", "invite")
    assert fake.muted == []


@pytest.mark.asyncio
async def test_remove_mute_failure_does_not_fail_remove(mod, fake):
    # a mute failure (e.g. the track vanished mid-race) must not break the
    # demote itself; the permission revoke already happened and is authoritative.
    fake.set_participant("alice", json.dumps({"hand_raised": True, "invited_to_stage": True}))
    fake._participants["alice"].tracks = [FakeTrack("TR_mic", api.TrackSource.MICROPHONE)]

    async def boom(req):
        raise RuntimeError("track already gone")

    fake.mute_published_track = boom
    cp = await mod.stage_action("space-x", "alice", "remove")
    assert cp is False


@pytest.mark.asyncio
async def test_kick_removes_participant(mod, fake):
    await mod.kick("space-x", "troll")
    assert fake.removed == ["troll"]


@pytest.mark.asyncio
async def test_mute_mutes_track(mod, fake):
    await mod.mute("space-x", "loud", "TR_abc")
    assert fake.muted == [("loud", "TR_abc", True)]


@pytest.mark.asyncio
async def test_concurrent_raise_and_invite_converge(mod, fake, monkeypatch):
    # force a scheduler yield between read and write so an unlocked impl would
    # interleave and lose a flag; the per-identity lock must serialize them.
    orig_get = fake.get_participant

    async def slow_get(req):
        await asyncio.sleep(0)
        return await orig_get(req)

    monkeypatch.setattr(fake, "get_participant", slow_get)
    fake.set_participant("alice", "")  # starts off-stage

    await asyncio.gather(
        mod.stage_action("space-x", "alice", "raise_hand"),
        mod.stage_action("space-x", "alice", "invite"),
    )
    final = json.loads(fake.updates[-1].metadata)
    assert final["hand_raised"] is True
    assert final["invited_to_stage"] is True  # neither write clobbered the other


@pytest.mark.asyncio
async def test_lock_dict_does_not_grow_unbounded(mod, fake):
    # many distinct identities, each fully serialized → the per-identity lock must
    # be evicted once released (uncontended), so the dict stays bounded.
    for i in range(200):
        await mod.stage_action("space-x", f"user-{i}", "raise_hand")
    assert len(mod._locks) == 0


@pytest.mark.asyncio
async def test_contended_lock_is_retained_then_evicted(mod, fake, monkeypatch):
    # a contended lock must survive the first waiter's release (the second waiter
    # still holds it), and be gone once both finish.
    orig_get = fake.get_participant

    async def slow_get(req):
        await asyncio.sleep(0)
        return await orig_get(req)

    monkeypatch.setattr(fake, "get_participant", slow_get)
    fake.set_participant("alice", "")
    await asyncio.gather(
        mod.stage_action("space-x", "alice", "raise_hand"),
        mod.stage_action("space-x", "alice", "invite"),
    )
    assert ("space-x", "alice") not in mod._locks  # cleaned up after both done


@pytest.mark.asyncio
async def test_aclose_noop_when_client_never_built():
    m = Moderator("ws://test:7880", "k", "s")  # no injected service, never used
    await m.aclose()  # must not raise


@pytest.mark.asyncio
async def test_set_sharing_false_sets_mic_only_sources(mod, fake):
    # host disables a speaker's video sharing: canPublishSources becomes
    # mic-only, but can_publish/can_subscribe/can_publish_data stay true so
    # the speaker can still talk.
    fake.set_participant("alice", json.dumps({"hand_raised": True, "invited_to_stage": True}))
    sharing = await mod.set_sharing("space-x", "alice", False)
    assert sharing is False
    perm = fake.updates[-1].permission
    assert list(perm.can_publish_sources) == [api.TrackSource.MICROPHONE]
    assert perm.can_publish is True
    assert perm.can_subscribe is True
    assert perm.can_publish_data is True


@pytest.mark.asyncio
async def test_set_sharing_true_restores_full_sources(mod, fake):
    fake.set_participant("alice", json.dumps({"hand_raised": True, "invited_to_stage": True}))
    sharing = await mod.set_sharing("space-x", "alice", True)
    assert sharing is True
    perm = fake.updates[-1].permission
    assert list(perm.can_publish_sources) == [
        api.TrackSource.MICROPHONE,
        api.TrackSource.CAMERA,
        api.TrackSource.SCREEN_SHARE,
        api.TrackSource.SCREEN_SHARE_AUDIO,
    ]
    assert perm.can_publish is True


@pytest.mark.asyncio
async def test_set_sharing_preserves_existing_stage_metadata(mod, fake):
    # set_sharing must NOT clobber the raise-hand/invited stage state carried
    # in metadata; it only touches canPublishSources.
    meta = json.dumps({"hand_raised": True, "invited_to_stage": True})
    fake.set_participant("alice", meta)
    await mod.set_sharing("space-x", "alice", False)
    assert fake.updates[-1].metadata == meta


@pytest.mark.asyncio
async def test_set_sharing_unknown_identity_is_graceful(mod, fake):
    # no prior participant record; must not raise, still applies mic-only.
    sharing = await mod.set_sharing("space-x", "ghost", False)
    assert sharing is False
    perm = fake.updates[-1].permission
    assert list(perm.can_publish_sources) == [api.TrackSource.MICROPHONE]


@pytest.mark.asyncio
async def test_set_sharing_false_force_stops_live_video_tracks(mod, fake):
    # backstop (mirrors the M5 remove/mic backstop): a frozen or
    # non-cooperative client may ignore the canPublishSources revoke above and
    # keep publishing screen/camera, so allow=false must also force-stop any
    # already-published CAMERA/SCREEN_SHARE/SCREEN_SHARE_AUDIO track. The mic
    # track must be left alone (speaker can still talk).
    fake.set_participant("alice", json.dumps({"hand_raised": True, "invited_to_stage": True}))
    fake._participants["alice"].tracks = [
        FakeTrack("TR_mic", api.TrackSource.MICROPHONE),
        FakeTrack("TR_cam", api.TrackSource.CAMERA),
        FakeTrack("TR_screen", api.TrackSource.SCREEN_SHARE),
        FakeTrack("TR_screen_audio", api.TrackSource.SCREEN_SHARE_AUDIO),
    ]
    await mod.set_sharing("space-x", "alice", False)
    assert sorted(fake.muted) == sorted(
        [
            ("alice", "TR_cam", True),
            ("alice", "TR_screen", True),
            ("alice", "TR_screen_audio", True),
        ]
    )


@pytest.mark.asyncio
async def test_set_sharing_false_with_no_published_video_track_is_noop(mod, fake):
    # best effort: no published video track means no mute call and no error.
    fake.set_participant("alice", json.dumps({"hand_raised": True, "invited_to_stage": True}))
    fake._participants["alice"].tracks = [FakeTrack("TR_mic", api.TrackSource.MICROPHONE)]
    await mod.set_sharing("space-x", "alice", False)
    assert fake.muted == []


@pytest.mark.asyncio
async def test_set_sharing_true_does_not_force_stop_tracks(mod, fake):
    # allow=true must never force-stop anything, even with live video tracks.
    fake.set_participant("alice", json.dumps({"hand_raised": True, "invited_to_stage": True}))
    fake._participants["alice"].tracks = [
        FakeTrack("TR_cam", api.TrackSource.CAMERA),
        FakeTrack("TR_screen", api.TrackSource.SCREEN_SHARE),
    ]
    await mod.set_sharing("space-x", "alice", True)
    assert fake.muted == []


@pytest.mark.asyncio
async def test_set_sharing_false_mute_failure_does_not_fail_set_sharing(mod, fake):
    # a mute failure (e.g. the track vanished mid-race) must not break the
    # permission update itself; the canPublishSources revoke already happened
    # and is authoritative.
    fake.set_participant("alice", json.dumps({"hand_raised": True, "invited_to_stage": True}))
    fake._participants["alice"].tracks = [FakeTrack("TR_cam", api.TrackSource.CAMERA)]

    async def boom(req):
        raise RuntimeError("track already gone")

    fake.mute_published_track = boom
    sharing = await mod.set_sharing("space-x", "alice", False)
    assert sharing is False


@pytest.mark.asyncio
async def test_aclose_closes_injected_client():
    closed = []

    class FakeClient:
        async def aclose(self):
            closed.append(True)

    fc = FakeClient()
    m = Moderator("ws://test:7880", "k", "s", _room_service=fc)
    await m.aclose()
    assert closed == [True]
