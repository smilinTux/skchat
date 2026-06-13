import asyncio

import pytest
from skcomms.glossa import codec
from skcomms.glossa.codebook import default_codebook
from skcomms.glossa.handshake import CapabilityDescriptor
from skcomms.glossa.message import Message

from skchat.glossa_mesh.bus import FakeBus, FakeBusMedium
from skchat.glossa_mesh.node import GlossaMeshNode


def _node(fqid, medium):
    d = CapabilityDescriptor(fqid=fqid, model_tier="large", max_level=codec.L2_CODEBOOK,
                             codebook_version=default_codebook().version, lexicon_version="")
    return GlossaMeshNode(descriptor=d, bus=FakeBus(fqid, medium),
                          codebook=default_codebook())


@pytest.mark.asyncio
async def test_ten_agent_mesh_broadcast_reaches_all_and_glosses():
    medium = FakeBusMedium()
    fqids = [f"agent{i}@x.y" for i in range(10)]
    nodes = [_node(f, medium) for f in fqids]
    inboxes = {f: [] for f in fqids}
    for f, n in zip(fqids, nodes):
        n.on_message(lambda src, m, _f=f: inboxes[_f].append(m))
    for n in nodes:
        await n.start()
    for n in nodes:
        await n.announce()
    await asyncio.sleep(0.05)

    speaker = nodes[0]
    msg = Message(intent="status.report", args={"oof": 42}, text="nominal")
    await speaker.say(msg)
    await asyncio.sleep(0.05)

    # every OTHER agent received it; the speaker did not receive its own
    for f in fqids[1:]:
        assert inboxes[f] == [msg]
    assert inboxes[fqids[0]] == []
    # the speaker's audit log holds the human-readable gloss
    assert any("status.report" in line for line in speaker.audit_log)
    # a listener's audit log glosses the inbound traffic to English
    assert any("status.report" in line for line in nodes[1].audit_log)
