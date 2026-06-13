import pytest

from skchat.spaces.recording import Recorder


class FakeEgress:
    def __init__(self):
        self.started = []
        self.stopped = []

    async def start_room_composite_egress(self, req):
        self.started.append(req)
        class _R:  # minimal egress-info stand-in
            egress_id = "EG_test123"
        return _R()

    async def stop_egress(self, req):
        self.stopped.append(req.egress_id)


@pytest.fixture
def fake():
    return FakeEgress()


@pytest.fixture
def rec(fake):
    return Recorder("ws://test:7880", "k", "s", _egress=fake)


@pytest.mark.asyncio
async def test_start_returns_egress_id_and_is_audio_only(rec, fake):
    eid = await rec.start("space-x", "/tmp/space-x.ogg")
    assert eid == "EG_test123"
    req = fake.started[-1]
    assert req.room_name == "space-x"
    assert req.audio_only is True
    assert len(req.file_outputs) == 1


@pytest.mark.asyncio
async def test_stop_calls_stop_egress(rec, fake):
    await rec.stop("EG_test123")
    assert fake.stopped == ["EG_test123"]


@pytest.mark.asyncio
async def test_aclose_noop_when_client_never_built():
    r = Recorder("ws://test:7880", "k", "s")  # no injected egress, never used
    await r.aclose()  # must not raise


@pytest.mark.asyncio
async def test_aclose_closes_injected_client():
    closed = []

    class FakeClient:
        async def aclose(self):
            closed.append(True)

    r = Recorder("ws://test:7880", "k", "s", _egress=FakeClient())
    await r.aclose()
    assert closed == [True]
