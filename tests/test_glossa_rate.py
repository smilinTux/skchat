"""G2 rate adaptation: RateController hysteresis + ceiling clamp + mesh wiring."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("skcomms.glossa", reason="skcomms not installed")

from skcomms.glossa.codebook import default_codebook  # noqa: E402
from skcomms.glossa.handshake import CapabilityDescriptor  # noqa: E402
from skcomms.glossa.message import Message  # noqa: E402

from skchat.glossa_mesh import codec_ext  # noqa: E402
from skchat.glossa_mesh.bus import FakeBus, FakeBusMedium  # noqa: E402
from skchat.glossa_mesh.node import GlossaMeshNode  # noqa: E402
from skchat.glossa_mesh.rate import (  # noqa: E402
    RateController,
    quality_from_network,
)
from skchat.glossa_mesh.session import GlossaMeshSession  # noqa: E402


def test_starts_optimistic_at_max():
    rc = RateController(max_tier=3)
    assert rc.target == 3
    assert rc.level(ceiling=3) == 3


def test_degrades_fast_under_poor_conditions():
    rc = RateController(max_tier=3)  # starts at 3
    rc.observe(0.1)  # poor -> immediate one-step degrade
    assert rc.target == 2
    rc.observe(0.1)
    assert rc.target == 1
    rc.observe(0.1)
    assert rc.target == 0
    rc.observe(0.1)  # never below floor
    assert rc.target == 0


def test_upgrades_slowly_under_sustained_good():
    rc = RateController(max_tier=3, start=0, up_patience=3)
    assert rc.target == 0
    rc.observe(0.9)
    rc.observe(0.9)
    assert rc.target == 0  # patience not yet met
    rc.observe(0.9)
    assert rc.target == 1  # third good observation -> step up
    # neutral band does not build toward an upgrade
    rc.observe(0.5)
    rc.observe(0.5)
    rc.observe(0.5)
    assert rc.target == 1


def test_level_clamped_to_negotiated_ceiling():
    rc = RateController(max_tier=3)  # wants 3
    assert rc.level(ceiling=1) == 1  # weakest peer caps it, never denser
    assert rc.level(ceiling=0) == 0


def test_quality_from_network_monotonic():
    clean = quality_from_network(loss=0.0, latency_ms=0.0)
    lossy = quality_from_network(loss=0.5, latency_ms=0.0)
    laggy = quality_from_network(loss=0.0, latency_ms=400.0)
    assert clean == pytest.approx(1.0)
    assert 0.0 <= lossy < clean
    assert laggy == pytest.approx(0.0)


def test_observe_network_degrades_on_lossy_link():
    rc = RateController(max_tier=3)
    rc.observe_network(loss=0.6, latency_ms=300)  # poor
    assert rc.target == 2


def _desc(fqid: str, max_level: int) -> CapabilityDescriptor:
    return CapabilityDescriptor(fqid=fqid, model_tier="large", max_level=max_level,
                                codebook_version=default_codebook().version)


def test_session_rate_degrades_encoded_tier():
    cb = default_codebook()
    peer = _desc("b@x", 3)
    rc = RateController(max_tier=3)  # optimistic
    sess = GlossaMeshSession(descriptor=_desc("a@x", 3), codebook=cb,
                             peers=[peer], rate=rc)
    assert sess.effective_level == 3  # good link -> densest negotiated tier (L3)
    out_dense = sess.encode(Message(intent="ack"))
    assert out_dense["tier"] == 3
    # Poor conditions -> degrade; the very next frame is encoded at a lower tier.
    sess.observe(0.05)
    assert sess.effective_level == 2
    out_degraded = sess.encode(Message(intent="ack"))
    assert out_degraded["tier"] == 2
    # And it still round-trips at the degraded tier.
    assert sess.decode(out_degraded["wire"])["message"] == Message(intent="ack")


def test_session_rate_recovers_upward_when_good():
    cb = default_codebook()
    rc = RateController(max_tier=3, start=0, up_patience=2)
    sess = GlossaMeshSession(descriptor=_desc("a@x", 3), codebook=cb,
                             peers=[_desc("b@x", 3)], rate=rc)
    assert sess.effective_level == 0  # started degraded
    sess.observe(0.95)
    sess.observe(0.95)
    assert sess.effective_level == 1  # climbed back up under good conditions


def test_node_rate_wiring_end_to_end():
    async def run():
        cb = default_codebook()
        medium = FakeBusMedium()
        rc = RateController(max_tier=3)
        a = GlossaMeshNode(descriptor=_desc("a@x", 3), bus=FakeBus("a", medium),
                           codebook=cb, rate=rc)
        b = GlossaMeshNode(descriptor=_desc("b@x", 3), bus=FakeBus("b", medium),
                           codebook=cb)
        seen: list = []
        b.on_message(lambda src, m: seen.append((src, m)))
        await a.start()
        await b.start()
        await a.announce()
        await b.announce()
        assert a.effective_level == 3
        # degrade the sender, then send: receiver decodes at the frame's tier byte
        a.observe(0.02)
        assert a.effective_level == 2
        msg = Message(intent="status.report", args={"n": 1})
        await a.say(msg)
        assert seen == [("a", msg)]
        assert a.audit_log[-1].startswith("[tx L2]")
    asyncio.run(run())


def test_no_rate_controller_is_backward_compatible():
    cb = default_codebook()
    sess = GlossaMeshSession(descriptor=_desc("a@x", 2), codebook=cb,
                             peers=[_desc("b@x", 2)])
    assert sess.rate is None
    assert sess.effective_level == sess.group_level == 2
