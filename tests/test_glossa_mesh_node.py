import asyncio

import pytest

# skcomms (pulled in transitively by skchat.glossa_mesh) is an optional dep —
# skip the whole module if it is absent so collection stays clean.
pytest.importorskip("skcomms.glossa", reason="skcomms not installed")

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
    await a.start()
    await b.start()
    await a.announce()
    await b.announce()
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


@pytest.mark.asyncio
async def test_forget_peer_uncaps_group_level():
    medium = FakeBusMedium()
    a = _node("a@x.y", medium, max_level=codec.L2_CODEBOOK)
    b = _node("b@x.y", medium, max_level=codec.L0_ENGLISH)  # weak
    c = _node("c@x.y", medium, max_level=codec.L2_CODEBOOK)
    for n in (a, b, c):
        await n.start()
    for n in (a, b, c):
        await n.announce()
    await asyncio.sleep(0.03)
    assert a.group_level == codec.L0_ENGLISH  # capped to the weak peer
    a.forget_peer("b@x.y")                     # weak peer left
    assert a.group_level == codec.L2_CODEBOOK  # un-capped by remaining strong peers


@pytest.mark.asyncio
async def test_forget_unknown_peer_is_noop():
    medium = FakeBusMedium()
    a = _node("a@x.y", medium)
    a.forget_peer("nobody@x.y")  # must not raise


@pytest.mark.asyncio
async def test_solo_node_group_level_falls_back_to_own_max():
    """With no peers heard, group_level is the node's own max_level (not 0)."""
    medium = FakeBusMedium()
    a = _node("a@x.y", medium, max_level=codec.L2_CODEBOOK)
    await a.start()
    assert a.group_level == codec.L2_CODEBOOK


@pytest.mark.asyncio
async def test_audit_log_glosses_every_tx_and_rx():
    """AUDIT-GLOSS INVARIANT: every sent and every received message produces an
    English gloss line, even when no on_message handler is registered."""
    medium = FakeBusMedium()
    a = _node("a@x.y", medium)
    b = _node("b@x.y", medium)  # deliberately NO on_message handler
    await a.start()
    await b.start()
    await a.announce()
    await b.announce()
    await asyncio.sleep(0.02)

    await a.say(Message(intent="coord.claim", args={"task": "t1"}))
    await asyncio.sleep(0.02)

    # Sender logged a [tx ...] gloss with the intent in plain English.
    tx_lines = [ln for ln in a.audit_log if ln.startswith("[tx")]
    assert len(tx_lines) == 1
    assert "coord.claim" in tx_lines[0]
    # Receiver logged a [rx ...] gloss naming the sender — without a handler set.
    rx_lines = [ln for ln in b.audit_log if ln.startswith("[rx")]
    assert len(rx_lines) == 1
    assert "a@x.y" in rx_lines[0]
    assert "coord.claim" in rx_lines[0]


@pytest.mark.asyncio
async def test_audit_log_records_tx_level():
    """The tx gloss records the level the message was actually encoded at."""
    medium = FakeBusMedium()
    a = _node("a@x.y", medium, max_level=codec.L2_CODEBOOK)
    b = _node("b@x.y", medium, max_level=codec.L0_ENGLISH)  # weak → caps to L0
    await a.start()
    await b.start()
    await a.announce()
    await b.announce()
    await asyncio.sleep(0.02)

    await a.say(Message(intent="ack"))
    await asyncio.sleep(0.02)
    assert any(f"[tx L{codec.L0_ENGLISH}]" in ln for ln in a.audit_log)


@pytest.mark.asyncio
async def test_malformed_frames_are_ignored_not_crashing():
    """Empty / unknown-kind / undecodable frames are swallowed; the node survives
    and never registers junk as a peer."""
    medium = FakeBusMedium()
    a = _node("a@x.y", medium)
    await a.start()

    a._on_frame(b"", "junk@x.y")            # empty frame → parse error
    a._on_frame(bytes([99]) + b"xx", "junk@x.y")  # unknown kind byte
    a._on_frame(bytes([1]), "junk@x.y")     # MESSAGE with empty payload

    assert "junk@x.y" not in a._peers
    assert a.audit_log == []  # nothing was decoded/glossed


@pytest.mark.asyncio
async def test_malformed_announce_does_not_register_peer():
    """A MESSAGE-kind body that isn't valid codec data is dropped without
    affecting group_level or the audit log."""
    medium = FakeBusMedium()
    a = _node("a@x.y", medium, max_level=codec.L2_CODEBOOK)
    await a.start()
    # ANNOUNCE byte + non-JSON payload → read_announce raises → swallowed.
    a._on_frame(bytes([0]) + b"not-json", "bad@x.y")
    assert "bad@x.y" not in a._peers
    assert a.group_level == codec.L2_CODEBOOK  # unaffected


@pytest.mark.asyncio
async def test_on_leave_callback_uncaps_via_simulated_leave():
    medium = FakeBusMedium()
    a = _node("a@x.y", medium, max_level=codec.L2_CODEBOOK)
    b = _node("b@x.y", medium, max_level=codec.L0_ENGLISH)  # weak
    for n in (a, b):
        await n.start()
    for n in (a, b):
        await n.announce()
    await asyncio.sleep(0.02)
    assert a.group_level == codec.L0_ENGLISH
    # the weak peer leaves the room → other members' on_leave fires
    await medium.simulate_leave("b@x.y")
    assert a.group_level == codec.L2_CODEBOOK
