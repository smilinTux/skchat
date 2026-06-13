import asyncio

import pytest

from skchat.glossa_mesh.bus import FakeBus, FakeBusMedium


@pytest.mark.asyncio
async def test_broadcast_reaches_all_other_members():
    medium = FakeBusMedium()
    a, b, c = FakeBus("a", medium), FakeBus("b", medium), FakeBus("c", medium)
    got_b, got_c = [], []
    b.on_receive(lambda data, src: got_b.append((data, src)))
    c.on_receive(lambda data, src: got_c.append((data, src)))
    await a.start()
    await b.start()
    await c.start()
    await a.broadcast(b"hi-mesh")
    await asyncio.sleep(0.01)
    assert got_b == [(b"hi-mesh", "a")]
    assert got_c == [(b"hi-mesh", "a")]


@pytest.mark.asyncio
async def test_sender_does_not_receive_its_own_broadcast():
    medium = FakeBusMedium()
    a = FakeBus("a", medium)
    got = []
    a.on_receive(lambda data, src: got.append(data))
    await a.start()
    await a.broadcast(b"x")
    await asyncio.sleep(0.01)
    assert got == []
