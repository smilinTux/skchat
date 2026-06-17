import asyncio

import pytest

# skcomms (pulled in transitively by skchat.glossa_mesh) is an optional dep —
# skip the whole module if it is absent so collection stays clean.
pytest.importorskip("skcomms.glossa", reason="skcomms not installed")

from skcomms.glossa import codec
from skcomms.glossa.codebook import default_codebook
from skcomms.glossa.handshake import CapabilityDescriptor
from skcomms.glossa.message import Message

from skchat.glossa_mesh.audio_bus import AudioMeshBus
from skchat.glossa_mesh.mac import FakeAudioMedium
from skchat.glossa_mesh.node import GlossaMeshNode


def _node(fqid, med):
    d = CapabilityDescriptor(fqid=fqid, model_tier="large", max_level=codec.L1_SCHEMA,
                             codebook_version=default_codebook().version, lexicon_version="")
    return GlossaMeshNode(descriptor=d, bus=AudioMeshBus(fqid, med),
                          codebook=default_codebook())


@pytest.mark.asyncio
async def test_skglossa_message_over_the_audio_mesh():
    med = FakeAudioMedium()
    a, b = _node("a@x.y", med), _node("b@x.y", med)
    inbox = []
    b.on_message(lambda fqid, m: inbox.append(m))
    await a.start()
    await b.start()
    await a.announce()
    await b.announce()
    await asyncio.sleep(0.02)
    await a.say(Message(intent="ack"))
    await asyncio.sleep(0.02)
    assert inbox == [Message(intent="ack")]    # SKGlossa, modulated to tones, decoded
