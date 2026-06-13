import asyncio

import pytest

from skcomms.glossa import codec
from skcomms.glossa.codebook import default_codebook
from skcomms.glossa.handshake import CapabilityDescriptor
from skcomms.glossa.message import Message
from skchat.glossa_mesh.bus import FakeBus, FakeBusMedium
from skchat.glossa_mesh.node import GlossaMeshNode


def _desc(fqid, max_level):
    return CapabilityDescriptor(fqid=fqid, model_tier="large", max_level=max_level,
                                codebook_version=default_codebook().version,
                                lexicon_version="")


def _node(fqid, medium, max_level=codec.L2_CODEBOOK):
    return GlossaMeshNode(descriptor=_desc(fqid, max_level),
                          bus=FakeBus(fqid, medium), codebook=default_codebook())


@pytest.mark.asyncio
async def test_two_nodes_announce_handshake_and_exchange():
    medium = FakeBusMedium()
    a, b = _node("a@x.y", medium), _node("b@x.y", medium)
    inbox = []
    b.on_message(lambda fqid, m: inbox.append((fqid, m)))
    await a.start(); await b.start()
    await a.announce(); await b.announce()
    await asyncio.sleep(0.02)
    assert a.group_level == codec.L2_CODEBOOK  # both strong → L2

    await a.say(Message(intent="coord.claim", args={"task": "abc"}))
    await asyncio.sleep(0.02)
    assert inbox == [("a@x.y", Message(intent="coord.claim", args={"task": "abc"}))]


@pytest.mark.asyncio
async def test_weakest_peer_caps_group_level():
    medium = FakeBusMedium()
    a = _node("a@x.y", medium, max_level=codec.L2_CODEBOOK)
    b = _node("b@x.y", medium, max_level=codec.L0_ENGLISH)  # weak
    c = _node("c@x.y", medium, max_level=codec.L2_CODEBOOK)
    inbox_b, inbox_c = [], []
    b.on_message(lambda f, m: inbox_b.append(m))
    c.on_message(lambda f, m: inbox_c.append(m))
    for n in (a, b, c):
        await n.start()
    for n in (a, b, c):
        await n.announce()
    await asyncio.sleep(0.03)
    assert a.group_level == codec.L0_ENGLISH  # capped to the weak peer
    await a.say(Message(intent="ack"))
    await asyncio.sleep(0.02)
    assert inbox_b == [Message(intent="ack")]   # the weak peer still decodes
    assert inbox_c == [Message(intent="ack")]
