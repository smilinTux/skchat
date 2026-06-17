#!/usr/bin/env python3
"""Preflight the U9 GlossaMesh end-to-end over a FakeBus — no live SFU.

This script composes the REAL shipped components (it does NOT re-implement
them) and exercises the full U9 mesh contract before anyone burns a live
LiveKit room + two real agents:

* :class:`skchat.glossa_mesh.session_daemon.GlossaMeshSessionDaemon` — the
  production per-Space caller.
* :class:`skchat.glossa_mesh.gatekeeper.GlossaMeshGatekeeper` — the REAL
  capauth source-authentication wrapper (signs outbound, source-auths inbound).
* :class:`skchat.glossa_mesh.node.GlossaMeshNode` + ``protocol`` — the REAL
  N-way mesh node and wire framing.
* :class:`skcomms.glossa` ``codec`` / ``gloss`` / ``codebook`` / ``handshake``
  — the REAL semantic codec.

The ONLY fakes are at the true external boundary:

* the BUS — :class:`skchat.glossa_mesh.bus.FakeBus` (a shared in-memory
  broadcast medium standing in for the LiveKit data-channel / SFU); and
* the advocacy + memory COLLABORATORS — a recording advocacy double and a
  list-appending memory sink (standing in for ``AdvocacyEngine`` and the
  ``MemoryBridge`` capture leg).

The gatekeeper's sign/verify backend is an in-memory capauth stand-in that
mirrors the real backend shape (``sign(bytes)->str`` / ``verify(bytes,sig)->
fqid|None``) — the gatekeeper crypto path itself is the REAL code under test.

Checks (each must pass or the script exits non-zero):

  1. ENROLL      — two daemons announce + see each other (group density).
  2. VERIFIED    — node A say() → node B receives a VERIFIED frame, dispatches
                   to the mock advocacy + captures the English audit gloss in
                   the mock memory.
  3. TAMPERED    — a forged-body frame is dropped at the gatekeeper, PRE-decode
                   (never reaches advocacy/memory).
  4. FORGED-SRC  — a source-spoofed frame is dropped (anti-spoof FQID binding).
  5. LEAVE       — a peer-disconnect fires forget_peer → the room un-caps.

On success prints ``PASS`` + a "TO GO LIVE" block. Exit 0 on PASS, 1 on any
failure.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys

from skcomms.glossa import codec, gloss
from skcomms.glossa.codebook import default_codebook
from skcomms.glossa.handshake import CapabilityDescriptor
from skcomms.glossa.message import Message

from skchat.glossa_mesh import protocol
from skchat.glossa_mesh.bus import FakeBus, FakeBusMedium
from skchat.glossa_mesh.gatekeeper import GlossaMeshGatekeeper
from skchat.glossa_mesh.session_daemon import GlossaMeshSessionDaemon
from skchat.spaces.space import Space

# --------------------------------------------------------------------------- #
# fakes — ONLY at the external boundary (bus medium / advocacy / memory / key)
# --------------------------------------------------------------------------- #


class FakeKeyring:
    """In-memory capauth stand-in mirroring the real backend's sign/verify shape.

    ``sign(data)->str`` binds the signer's fqid + an HMAC-shaped tag; ``verify``
    recovers the authenticating fqid iff the tag matches. This is the boundary
    the REAL gatekeeper crypto path is driven against.
    """

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


class MockAdvocacy:
    """Advocacy double: records every dispatched (sender, gloss) message."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []

    def process_message(self, msg) -> str | None:
        self.seen.append((msg.sender, msg.content))
        return None


# --------------------------------------------------------------------------- #
# builders — REAL components, fake bus/gatekeeper-backend/collaborators
# --------------------------------------------------------------------------- #


def _descriptor(fqid: str, max_level: int) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        fqid=fqid,
        model_tier="large",
        max_level=max_level,
        codebook_version=default_codebook().version,
        lexicon_version="",
    )


def _gatekeeper(fqid: str) -> GlossaMeshGatekeeper:
    # REAL gatekeeper; only the sign/verify backend is the in-memory stand-in.
    kr = FakeKeyring(fqid)
    return GlossaMeshGatekeeper(
        source_fqid=fqid, signer=kr.signer, verifier=FakeKeyring.verifier
    )


def _space() -> Space:
    return Space(
        space_id="space-preflight", host_fqid="lumina@skworld.io", title="Preflight", slug="pf"
    )


def _daemon(
    fqid: str,
    medium: FakeBusMedium,
    *,
    max_level: int = codec.L2_CODEBOOK,
    advocacy=None,
    memory=None,
) -> GlossaMeshSessionDaemon:
    # REAL daemon wraps REAL node + REAL gatekeeper over a FakeBus.
    return GlossaMeshSessionDaemon(
        space=_space(),
        descriptor=_descriptor(fqid, max_level),
        bus=FakeBus(fqid, medium),
        gatekeeper=_gatekeeper(fqid),
        advocacy=advocacy,
        memory=memory,
    )


# --------------------------------------------------------------------------- #
# check harness
# --------------------------------------------------------------------------- #


class CheckFailure(AssertionError):
    pass


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise CheckFailure(msg)


def _ok(label: str, detail: str = "") -> None:
    tail = f" — {detail}" if detail else ""
    print(f"  [ok] {label}{tail}")


# --------------------------------------------------------------------------- #
# the five end-to-end checks
# --------------------------------------------------------------------------- #


async def check_enroll() -> None:
    medium = FakeBusMedium()
    a = _daemon("agentA@skworld.io", medium)
    b = _daemon("agentB@skworld.io", medium)
    await a.start()  # start() announces capabilities to the mesh
    await b.start()
    _require(
        a.group_level == codec.L2_CODEBOOK,
        f"A density {a.group_level} != full {codec.L2_CODEBOOK} after mutual announce",
    )
    _require(
        b.group_level == codec.L2_CODEBOOK,
        f"B density {b.group_level} != full {codec.L2_CODEBOOK} after mutual announce",
    )
    _ok("ENROLL", f"both nodes announced + negotiated to L{a.group_level}")


async def check_verified_dispatch() -> None:
    medium = FakeBusMedium()
    captured: list[tuple[str, str]] = []
    advocacy = MockAdvocacy()
    a = _daemon("agentA@skworld.io", medium)
    b = _daemon(
        "agentB@skworld.io",
        medium,
        advocacy=advocacy,
        memory=lambda text, src: captured.append((src, text)),
    )
    await a.start()
    await b.start()

    msg = Message(intent="coord.claim", args={"task": "preflight-glossa"})
    await a.say(msg)

    _require(
        b.dispatched == [("agentA@skworld.io", msg)],
        f"B dispatched {b.dispatched!r}, expected one verified msg from A",
    )
    _require(len(captured) == 1, f"memory captured {len(captured)} items, expected 1")
    src, text = captured[0]
    expected_gloss = gloss.to_english(msg)
    _require(src == "agentA@skworld.io", f"memory src {src!r} != A")
    _require(
        text == expected_gloss,
        f"memory captured {text!r}, expected English audit gloss {expected_gloss!r}",
    )
    _require(
        advocacy.seen == [("agentA@skworld.io", expected_gloss)],
        f"advocacy saw {advocacy.seen!r}, expected [(A, gloss)]",
    )
    _ok("VERIFIED", f"B dispatched A's frame → advocacy + memory gloss {text!r}")


async def check_tampered_dropped() -> None:
    medium = FakeBusMedium()
    captured: list = []
    advocacy = MockAdvocacy()
    a = _daemon("agentA@skworld.io", medium)
    b = _daemon(
        "agentB@skworld.io",
        medium,
        advocacy=advocacy,
        memory=lambda t, s: captured.append((s, t)),
    )
    await a.start()
    await b.start()

    # A signs a real frame; we tamper the body so the signature no longer covers
    # it, then inject it straight onto B's RAW transport (the inner FakeBus).
    good = a.gatekeeper.wrap_outbound(protocol.frame_message(2, b"orig"))
    env = json.loads(good.decode())
    env["frame"] = base64.b64encode(protocol.frame_message(2, b"EVIL")).decode("ascii")
    tampered = json.dumps(env).encode()
    b.node.bus._inner._inbound(tampered, "agentA@skworld.io")  # type: ignore[attr-defined]

    _require(b.dispatched == [], f"tampered frame leaked to dispatch: {b.dispatched!r}")
    _require(captured == [], f"tampered frame reached memory: {captured!r}")
    _require(advocacy.seen == [], f"tampered frame reached advocacy: {advocacy.seen!r}")
    _ok("TAMPERED", "forged-body frame dropped at gatekeeper, pre-decode")


async def check_forged_source_dropped() -> None:
    medium = FakeBusMedium()
    captured: list = []
    advocacy = MockAdvocacy()
    a = _daemon("agentA@skworld.io", medium)
    b = _daemon(
        "agentB@skworld.io",
        medium,
        advocacy=advocacy,
        memory=lambda t, s: captured.append((s, t)),
    )
    await a.start()
    await b.start()

    # A signs correctly, then rewrites the claimed source to masquerade as host.
    signed = a.gatekeeper.wrap_outbound(protocol.frame_message(2, b"hi"))
    env = json.loads(signed.decode())
    env["source"] = "lumina@skworld.io"  # masquerade as the Space host
    forged = json.dumps(env).encode()
    b.node.bus._inner._inbound(forged, "agentA@skworld.io")  # type: ignore[attr-defined]

    _require(b.dispatched == [], f"forged-source frame leaked: {b.dispatched!r}")
    _require(captured == [], f"forged-source frame reached memory: {captured!r}")
    _require(advocacy.seen == [], f"forged-source frame reached advocacy: {advocacy.seen!r}")
    _ok("FORGED-SRC", "source-spoofed frame dropped (anti-spoof FQID binding)")


async def check_peer_leave_uncaps() -> None:
    medium = FakeBusMedium()
    strong = _daemon("strong@skworld.io", medium, max_level=codec.L2_CODEBOOK)
    weak = _daemon("weak@skworld.io", medium, max_level=codec.L0_ENGLISH)
    await strong.start()
    await weak.start()

    capped = strong.group_level
    _require(
        capped < codec.L2_CODEBOOK,
        f"weak peer should cap room below L{codec.L2_CODEBOOK}, got L{capped}",
    )

    # weak peer disconnects → participant_disconnected analogue → forget_peer.
    await medium.simulate_leave("weak@skworld.io")
    recovered = strong.group_level
    _require(
        recovered == codec.L2_CODEBOOK,
        f"room did not un-cap after leave: L{recovered} != L{codec.L2_CODEBOOK}",
    )
    _ok("LEAVE", f"peer-leave un-capped room L{capped} → L{recovered} (forget_peer)")


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #


TO_GO_LIVE = """\
TO GO LIVE (per runbooks/glossa-over-space.md, Leg D):
  1. Stand up the LiveKit SFU on the tailnet and create a real Space:
       skchat spaces create --title ... --slug ...   (sk-lk-authd issues tokens)
  2. Replace FakeBus with skchat.glossa_mesh.livekit_bus.LiveKitBus built from
     the room url + per-agent token; replace FakeKeyring with the agent's REAL
     capauth identity (per-agent signing key — NEVER the operator key).
  3. Replace the mock advocacy/memory with the real skchat.advocacy.AdvocacyEngine
     and a skchat.memory_bridge.MemoryBridge capture wrapper.
  4. Spawn two real agents (e.g. lumina + opus) via spawn_session_for_space();
     have A announce + say, confirm B logs a VERIFIED inbound + the audit gloss
     lands in skmem-pg, and that a leave un-caps the room over the live SFU."""


async def _run() -> int:
    print("== GlossaMesh U9 preflight (FakeBus, no live SFU) ==")
    print("composing REAL: SessionDaemon + Gatekeeper + Node + protocol + skcomms.glossa")
    print("faking ONLY: bus medium + advocacy/memory collaborators + capauth backend\n")
    checks = (
        check_enroll,
        check_verified_dispatch,
        check_tampered_dropped,
        check_forged_source_dropped,
        check_peer_leave_uncaps,
    )
    try:
        for chk in checks:
            await chk()
    except CheckFailure as exc:
        print(f"\n  [FAIL] {exc}")
        print("\nFAIL — U9 mesh preflight did not hold; do NOT go live.")
        return 1
    except Exception as exc:  # noqa: BLE001 — any unexpected error is a hard fail
        print(f"\n  [ERROR] {type(exc).__name__}: {exc}")
        print("\nFAIL — U9 mesh preflight errored; do NOT go live.")
        return 1

    print("\nPASS — U9 GlossaMesh holds end-to-end over the FakeBus.")
    print("  verified-only dispatch, tamper/forge rejected pre-decode, leave un-caps.\n")
    print(TO_GO_LIVE)
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
