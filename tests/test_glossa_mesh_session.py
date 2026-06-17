"""Tests for GlossaMeshSessionDaemon (U9, wave-5) — per-Space daemon orchestration.

Everything is driven against a FakeBus + an in-memory fake capauth keyring, so no
live LiveKit room / SFU is needed. The daemon composes the already-CI-proven
GlossaMeshNode + GlossaMeshGatekeeper and adds the advocacy + memory dispatch and
the spawn hook.

Coverage:
  * enroll: start() announces and two daemons see each other (group_level).
  * inbound frame → verified → advocacy + memory dispatch.
  * tampered / forged-source frame is rejected before it reaches advocacy/memory.
  * peer-leave un-caps the room (forget_peer via on_leave).
  * outbound signing: every meshed frame is a valid gatekeeper envelope.
"""

from __future__ import annotations

import base64
import json

import pytest

pytest.importorskip("skcomms.glossa", reason="skcomms not installed")

from skcomms.glossa import codec
from skcomms.glossa.codebook import default_codebook
from skcomms.glossa.handshake import CapabilityDescriptor
from skcomms.glossa.message import Message

from skchat.glossa_mesh import protocol
from skchat.glossa_mesh.bus import FakeBus, FakeBusMedium
from skchat.glossa_mesh.gatekeeper import GlossaMeshGatekeeper
from skchat.glossa_mesh.session_daemon import (
    GlossaMeshSessionDaemon,
    spawn_session_for_space,
)
from skchat.spaces.space import Space

# --------------------------------------------------------------------------- #
# fakes / builders
# --------------------------------------------------------------------------- #


class FakeKeyring:
    """In-memory capauth stand-in (mirrors the gatekeeper-test fake)."""

    def __init__(self, fqid: str) -> None:
        self.fqid = fqid

    @staticmethod
    def _tag(data: bytes) -> str:
        return base64.b64encode(data).decode("ascii")

    def signer(self, data: bytes) -> str:
        return f"{self.fqid}|{self._tag(data)}"

    @staticmethod
    def verifier(data: bytes, sig: str) -> str | None:
        try:
            fqid, tag = sig.split("|", 1)
        except ValueError:
            return None
        if tag != FakeKeyring._tag(data):
            return None
        return fqid


class RecordingAdvocacy:
    """Advocacy double: records seen messages, optionally replies on @mention."""

    def __init__(self, reply: str | None = None) -> None:
        self.reply = reply
        self.seen: list[tuple[str, str]] = []

    def process_message(self, msg) -> str | None:
        self.seen.append((msg.sender, msg.content))
        if self.reply and "@lumina" in msg.content.lower():
            return self.reply
        return None


def _desc(fqid: str, max_level: int = codec.L2_CODEBOOK) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        fqid=fqid,
        model_tier="large",
        max_level=max_level,
        codebook_version=default_codebook().version,
        lexicon_version="",
    )


def _gatekeeper(fqid: str) -> GlossaMeshGatekeeper:
    # shared verify scheme across nodes; signer is keyed to the node's own fqid.
    return GlossaMeshGatekeeper(
        source_fqid=fqid, signer=FakeKeyring(fqid).signer, verifier=FakeKeyring.verifier
    )


def _space(host: str = "lumina@x.y") -> Space:
    return Space(space_id="space-test01", host_fqid=host, title="QA", slug="qa")


def _daemon(
    fqid: str,
    medium: FakeBusMedium,
    *,
    max_level: int = codec.L2_CODEBOOK,
    advocacy=None,
    memory=None,
) -> GlossaMeshSessionDaemon:
    return GlossaMeshSessionDaemon(
        space=_space(),
        descriptor=_desc(fqid, max_level),
        bus=FakeBus(fqid, medium),
        gatekeeper=_gatekeeper(fqid),
        advocacy=advocacy,
        memory=memory,
    )


# --------------------------------------------------------------------------- #
# 1. enroll
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_enroll_announces_and_peers_negotiate():
    medium = FakeBusMedium()
    a = _daemon("a@x.y", medium)
    b = _daemon("b@x.y", medium)
    await a.start()  # start() announces
    await b.start()
    # b heard a's announce and vice-versa → both at full density.
    assert a.group_level == codec.L2_CODEBOOK
    assert b.group_level == codec.L2_CODEBOOK
    assert a.fqid == "a@x.y"


def test_descriptor_fqid_must_match_gatekeeper():
    medium = FakeBusMedium()
    with pytest.raises(ValueError):
        GlossaMeshSessionDaemon(
            space=_space(),
            descriptor=_desc("a@x.y"),
            bus=FakeBus("a@x.y", medium),
            gatekeeper=_gatekeeper("someone-else@x.y"),
        )


# --------------------------------------------------------------------------- #
# 2. inbound verified → advocacy + memory dispatch
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_inbound_frame_verified_then_dispatched_to_advocacy_and_memory():
    medium = FakeBusMedium()
    captured: list[tuple[str, str]] = []
    advocacy = RecordingAdvocacy()
    a = _daemon("a@x.y", medium)
    b = _daemon(
        "b@x.y",
        medium,
        advocacy=advocacy,
        memory=lambda text, src: captured.append((src, text)),
    )
    await a.start()
    await b.start()

    msg = Message(intent="coord.claim", args={"task": "abc"})
    await a.say(msg)

    # b dispatched exactly one verified inbound message from a.
    assert b.dispatched == [("a@x.y", msg)]
    # memory captured the English audit gloss (not raw codec bytes), keyed by src.
    assert len(captured) == 1
    src, text = captured[0]
    assert src == "a@x.y"
    assert isinstance(text, str) and text
    # advocacy saw the same source + gloss.
    assert advocacy.seen == [("a@x.y", text)]


@pytest.mark.asyncio
async def test_advocacy_reply_is_meshed_back_in_band():
    medium = FakeBusMedium()
    # b replies when @lumina-mentioned; a should receive that reply as a frame.
    advocacy = RecordingAdvocacy(reply="on it")
    a = _daemon("a@x.y", medium)
    b = _daemon("b@x.y", medium, advocacy=advocacy)
    a_inbox: list[tuple[str, Message]] = []
    a.node.on_message(lambda src, m: a_inbox.append((src, m)))
    await a.start()
    await b.start()

    await a.say(Message(intent="say", text="hey @lumina ping"))

    # b enqueued + (under a running loop) drained the reply back onto the mesh.
    import asyncio

    await asyncio.sleep(0.02)
    assert b.pending_replies == []  # drained
    assert any(src == "b@x.y" and m.text == "on it" for src, m in a_inbox)


# --------------------------------------------------------------------------- #
# 3. tampered / forged-source frame rejected before advocacy/memory
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tampered_inbound_frame_is_rejected_and_never_dispatched():
    medium = FakeBusMedium()
    captured: list = []
    advocacy = RecordingAdvocacy()
    a = _daemon("a@x.y", medium)
    b = _daemon("b@x.y", medium, advocacy=advocacy, memory=lambda t, s: captured.append((s, t)))
    await a.start()
    await b.start()

    # Hand-craft a signed envelope from a, then tamper the frame body so the
    # signature no longer covers it, and put it straight on b's transport.
    good = a.gatekeeper.wrap_outbound(protocol.frame_message(2, b"orig"))
    env = json.loads(good.decode())
    env["frame"] = base64.b64encode(protocol.frame_message(2, b"EVIL")).decode("ascii")
    tampered = json.dumps(env).encode()

    b.node.bus._inner._inbound(tampered, "a@x.y")  # type: ignore[attr-defined]

    assert b.dispatched == []  # rejected at the gatekeeper, never decoded
    assert captured == []
    assert advocacy.seen == []


@pytest.mark.asyncio
async def test_forged_source_frame_is_rejected():
    medium = FakeBusMedium()
    advocacy = RecordingAdvocacy()
    a = _daemon("a@x.y", medium)
    b = _daemon("b@x.y", medium, advocacy=advocacy)
    await a.start()
    await b.start()

    # a signs correctly, then rewrites the claimed source to "boss@x.y".
    signed = a.gatekeeper.wrap_outbound(protocol.frame_message(2, b"hi"))
    env = json.loads(signed.decode())
    env["source"] = "boss@x.y"  # masquerade
    forged = json.dumps(env).encode()

    b.node.bus._inner._inbound(forged, "a@x.y")  # type: ignore[attr-defined]

    assert b.dispatched == []
    assert advocacy.seen == []


@pytest.mark.asyncio
async def test_unsigned_frame_is_rejected():
    medium = FakeBusMedium()
    b = _daemon("b@x.y", medium, advocacy=RecordingAdvocacy())
    await b.start()
    # raw (un-enveloped) mesh frame straight on the wire → not a gatekeeper envelope.
    b.node.bus._inner._inbound(protocol.frame_message(2, b"raw"), "x@x.y")  # type: ignore[attr-defined]
    assert b.dispatched == []


# --------------------------------------------------------------------------- #
# 4. peer-leave un-caps the room
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_peer_leave_uncaps_group_level():
    medium = FakeBusMedium()
    strong = _daemon("strong@x.y", medium, max_level=codec.L2_CODEBOOK)
    weak = _daemon("weak@x.y", medium, max_level=codec.L0_ENGLISH)
    await strong.start()
    await weak.start()

    # the weak peer caps the room below L2.
    assert strong.group_level < codec.L2_CODEBOOK

    # weak peer disconnects → participant_disconnected analogue fires on_leave →
    # forget_peer, and the density recovers to strong's own max.
    await medium.simulate_leave("weak@x.y")
    assert strong.group_level == codec.L2_CODEBOOK


# --------------------------------------------------------------------------- #
# 5. outbound signing
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_outbound_frames_are_signed_envelopes():
    medium = FakeBusMedium()
    a = _daemon("a@x.y", medium)
    # tap the RAW transport (inner FakeBus) to see exactly what hits the wire.
    on_wire: list[bytes] = []
    other = FakeBus("watcher@x.y", medium)
    other.on_receive(lambda data, src: on_wire.append(data))
    await other.start()
    await a.start()  # announce → one signed frame

    await a.say(Message(intent="say", text="hello"))

    assert len(on_wire) >= 2  # announce + say
    for raw in on_wire:
        env = json.loads(raw.decode())  # each is a gatekeeper envelope
        assert env["source"] == "a@x.y"
        assert env["sig"]
        # and the verifier authenticates it back to a@x.y over its frame.
        source, frame = a.gatekeeper.unwrap_inbound(raw)
        assert source == "a@x.y"
        assert frame  # the inner mesh frame survives the round-trip


# --------------------------------------------------------------------------- #
# 6. spawn hook
# --------------------------------------------------------------------------- #


def test_spawn_session_for_space_builds_a_daemon():
    medium = FakeBusMedium()
    space = _space()
    d = spawn_session_for_space(
        space=space,
        descriptor=_desc("a@x.y"),
        bus=FakeBus("a@x.y", medium),
        gatekeeper=_gatekeeper("a@x.y"),
    )
    assert isinstance(d, GlossaMeshSessionDaemon)
    assert d.space is space
    assert d.fqid == "a@x.y"
