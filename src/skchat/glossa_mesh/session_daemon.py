"""GlossaMeshSessionDaemon (spec U9, wave-5) — per-Space daemon orchestration.

This is the production caller the live-test runbook (``runbooks/glossa-over-space.md``
Leg D) gates on: the piece that puts a real agent onto a live :class:`Space` and
meshes SKGlossa over it with capauth source-authentication and an
advocacy + memory capture leg.

It composes the already-shipped CI-proven primitives — it does **not** re-implement
them:

* :class:`~skchat.glossa_mesh.node.GlossaMeshNode` — the N-way mesh (announce,
  weakest-peer density, decode + audit gloss). The node auto-wires ``on_receive``
  / ``on_leave`` on whatever bus it is handed.
* :class:`~skchat.glossa_mesh.gatekeeper.GlossaMeshGatekeeper` — signs every
  outbound frame under the local capauth identity and source-authenticates every
  inbound frame (anti-spoof: claimed source FQID must equal the signing identity).
* a :class:`~skchat.glossa_mesh.bus.MeshBus` (``LiveKitBus`` live; ``FakeBus`` in
  CI) — the reliable broadcast medium.

The crypto interposition is done by a private :class:`_SigningBus` adapter that
wraps the real transport: the node only ever sees the adapter, so

* node ``broadcast(raw_frame)`` → gatekeeper ``wrap_outbound`` → real bus, and
* real bus inbound (signed envelope) → gatekeeper ``unwrap_inbound`` → node, but
  **only after the source FQID is authenticated**. A forged-source or tampered
  frame is dropped before the node ever decodes it (never reaches advocacy/memory).

Note ``on_leave`` is forwarded **un-wrapped**: a ``participant_disconnected`` event
carries no glossa frame to verify, just the departed member id, and the node's
``forget_peer`` un-caps the room (the leave-un-cap invariant).

Every successfully verified inbound message is dispatched to:

* **advocacy** — an object with ``process_message(ChatMessage) -> str | None``
  (e.g. :class:`skchat.advocacy.AdvocacyEngine`); a non-empty reply is encoded and
  meshed back via :meth:`say` so the peer sees the response in-band.
* **memory** — any callable ``(text, source_fqid) -> None`` (e.g. a thin wrapper
  over :class:`skchat.memory_bridge.MemoryBridge`); the English audit gloss of the
  decoded frame is what gets captured (the human-readable view, never raw codec
  bytes).

ALL collaborators (bus, gatekeeper, node, advocacy, memory) are injectable so the
whole daemon is unit-testable against a :class:`~skchat.glossa_mesh.bus.FakeBus`
with no live LiveKit room / SFU.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional, Protocol

from skcomms.glossa import gloss
from skcomms.glossa.codebook import Codebook, default_codebook
from skcomms.glossa.handshake import CapabilityDescriptor
from skcomms.glossa.message import Message

from skchat.glossa_mesh.bus import LeaveCb, MeshBus, ReceiveCb
from skchat.glossa_mesh.gatekeeper import GatekeeperError, GlossaMeshGatekeeper
from skchat.glossa_mesh.node import GlossaMeshNode

logger = logging.getLogger(__name__)

# A memory sink: capture the English gloss of a decoded inbound frame.
MemorySink = Callable[[str, str], None]  # (gloss_text, source_fqid) -> None


class _Advocacy(Protocol):
    """The slice of skchat.advocacy.AdvocacyEngine the daemon needs."""

    def process_message(self, msg: object) -> Optional[str]: ...


class _SigningBus(MeshBus):
    """A MeshBus adapter that interposes a gatekeeper over a real transport.

    The wrapped node treats this as its bus. Outbound frames are signed; inbound
    envelopes are source-authenticated and unwrapped before the node sees them.
    Leave events pass through untouched (no frame to verify).
    """

    def __init__(self, *, inner: MeshBus, gatekeeper: GlossaMeshGatekeeper) -> None:
        self._inner = inner
        self._gk = gatekeeper
        self._cb: ReceiveCb | None = None

    @property
    def member_id(self) -> str:
        return getattr(self._inner, "member_id", self._gk.source_fqid)

    async def start(self) -> None:
        await self._inner.start()

    async def stop(self) -> None:
        await self._inner.stop()

    async def broadcast(self, data: bytes) -> None:
        # data is a raw mesh frame from the node; sign it before it hits the wire.
        await self._inner.broadcast(self._gk.wrap_outbound(data))

    def on_receive(self, cb: ReceiveCb) -> None:
        # the node registers its decode entry-point here; we wrap the inner bus so
        # only verified, unwrapped frames are delivered upward.
        self._cb = cb
        self._inner.on_receive(self._verify_inbound)

    def on_leave(self, cb: LeaveCb) -> None:
        # leave carries no frame — forward straight through to forget_peer.
        self._inner.on_leave(cb)

    def _verify_inbound(self, signed: bytes, transport_src: str) -> None:
        """Source-authenticate a signed envelope, then deliver the raw frame to the
        node under the AUTHENTICATED source (not the transport's claimed identity).

        A malformed/forged/tampered frame is dropped here and never reaches the
        node — so advocacy/memory only ever see source-authenticated content.
        """
        try:
            source, frame = self._gk.unwrap_inbound(signed)
        except GatekeeperError as exc:
            logger.warning(
                "glossa mesh: rejected inbound from transport-src=%r (%s: %s)",
                transport_src,
                type(exc).__name__,
                exc,
            )
            return
        if self._cb is not None:
            self._cb(frame, source)


class GlossaMeshSessionDaemon:
    """Per-Space glossa-mesh daemon: enroll a node, route verified inbound frames
    to advocacy + memory, sign outbound frames, un-cap on peer-leave.

    Args:
        space: the live :class:`~skchat.spaces.space.Space` this daemon serves. Its
            ``room`` is the LiveKit room name; kept for the live spawn hook.
        descriptor: this agent's capability descriptor (its FQID is the mesh
            identity and must match ``gatekeeper.source_fqid``).
        bus: the reliable broadcast transport (``LiveKitBus`` live / ``FakeBus`` in
            tests). INJECTED — the daemon never constructs a live room itself.
        gatekeeper: capauth sign/verify wrapper (INJECTED with the agent's *own*
            per-agent key, per the skcomms agent-signing-key fix — never the
            operator key).
        advocacy: object exposing ``process_message`` (default: a no-op). A
            non-empty reply is meshed back in-band via :meth:`say`.
        memory: callable ``(gloss_text, source_fqid) -> None`` capturing the audit
            gloss of every inbound frame (default: a no-op).
        codebook: the shared semantic codebook (default: ``default_codebook()``).
        node_factory: override for the node constructor (testing seam; default
            builds a real :class:`GlossaMeshNode`).
    """

    def __init__(
        self,
        *,
        space,
        descriptor: CapabilityDescriptor,
        bus: MeshBus,
        gatekeeper: GlossaMeshGatekeeper,
        advocacy: _Advocacy | None = None,
        memory: MemorySink | None = None,
        codebook: Codebook | None = None,
        node_factory: Callable[..., GlossaMeshNode] | None = None,
    ) -> None:
        if descriptor.fqid != gatekeeper.source_fqid:
            raise ValueError(
                f"descriptor fqid {descriptor.fqid!r} != gatekeeper source "
                f"{gatekeeper.source_fqid!r} — the daemon must sign as its own identity"
            )
        self.space = space
        self.descriptor = descriptor
        self.gatekeeper = gatekeeper
        self.advocacy = advocacy
        self.memory = memory
        self.codebook = codebook or default_codebook()
        self.dispatched: list[tuple[str, Message]] = []
        self.pending_replies: list[Message] = []

        self._signing_bus = _SigningBus(inner=bus, gatekeeper=gatekeeper)
        factory = node_factory or GlossaMeshNode
        # node wires on_receive/on_leave on the signing bus in its constructor; the
        # signing bus relays only verified frames up and leaves straight through.
        self.node: GlossaMeshNode = factory(
            descriptor=descriptor, bus=self._signing_bus, codebook=self.codebook
        )
        self.node.on_message(self._on_verified_message)

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Start the transport and announce this node's capabilities to the mesh."""
        await self.node.start()
        await self.node.announce()

    async def stop(self) -> None:
        await self.node.stop()

    @property
    def group_level(self) -> int:
        """The current negotiated mesh density (weakest-peer-caps)."""
        return self.node.group_level

    @property
    def fqid(self) -> str:
        return self.descriptor.fqid

    # -- outbound ------------------------------------------------------------

    async def say(self, m: Message) -> None:
        """Encode + mesh a message at the negotiated tier. The signing bus stamps a
        capauth signature on the outbound frame (source-binding)."""
        await self.node.say(m)

    # -- inbound dispatch ----------------------------------------------------

    def _on_verified_message(self, source: str, m: Message) -> None:
        """Route a source-authenticated, decoded inbound message to advocacy +
        memory. Only reached for frames that passed the gatekeeper."""
        self.dispatched.append((source, m))
        english = gloss.to_english(m)

        # memory capture: store the human-readable audit gloss, never raw bytes.
        if self.memory is not None:
            try:
                self.memory(english, source)
            except Exception as exc:  # noqa: BLE001 — a sink failure must not kill the mesh
                logger.warning("glossa mesh: memory capture failed: %s", exc)

        # advocacy: if the gloss @mentions us, mesh the reply back in-band.
        if self.advocacy is not None:
            try:
                reply = self.advocacy.process_message(self._as_chat_message(source, english))
            except Exception as exc:  # noqa: BLE001
                logger.warning("glossa mesh: advocacy failed: %s", exc)
                reply = None
            if reply:
                self._enqueue_reply(reply)

    def _enqueue_reply(self, reply: str) -> None:
        """Mesh an advocacy reply back as a glossa ``say`` frame.

        ``say`` is async and this dispatch runs from the (sync) bus receive
        callback, so schedule it on the running loop when there is one; otherwise
        stash it for the caller/test to drain via :attr:`pending_replies`.
        """
        msg = Message(intent="say", text=reply)
        self.pending_replies.append(msg)
        try:
            import asyncio

            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (sync test path) — left in pending_replies
        loop.create_task(self._drain_reply(msg))

    async def _drain_reply(self, msg: Message) -> None:
        if msg in self.pending_replies:
            self.pending_replies.remove(msg)
        await self.say(msg)

    @staticmethod
    def _as_chat_message(source: str, text: str):
        """Adapt a decoded glossa gloss to the ChatMessage shape advocacy expects.

        Imported lazily so the daemon module loads even where skchat.models is not
        importable; falls back to a tiny duck-typed shim with .sender/.content.
        """
        try:
            from skchat.models import ChatMessage

            return ChatMessage(sender=source, recipient="", content=text)
        except Exception:  # noqa: BLE001 — keep the mesh independent of model schema
            from types import SimpleNamespace

            return SimpleNamespace(sender=source, content=text)


def spawn_session_for_space(
    *,
    space,
    descriptor: CapabilityDescriptor,
    bus: MeshBus,
    gatekeeper: GlossaMeshGatekeeper,
    advocacy: _Advocacy | None = None,
    memory: MemorySink | None = None,
    codebook: Codebook | None = None,
) -> GlossaMeshSessionDaemon:
    """Thin spawn hook: build a daemon for ``space``.

    This is the seam the spaces layer calls when a Space goes live (the live wiring
    constructs ``bus`` as a :class:`~skchat.glossa_mesh.livekit_bus.LiveKitBus` from
    the room url/token and ``gatekeeper`` from the agent's capauth identity). It is
    deliberately transport-agnostic so CI passes a ``FakeBus`` + fake gatekeeper.
    """
    return GlossaMeshSessionDaemon(
        space=space,
        descriptor=descriptor,
        bus=bus,
        gatekeeper=gatekeeper,
        advocacy=advocacy,
        memory=memory,
        codebook=codebook,
    )
