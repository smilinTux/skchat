"""GlossaMeshNode (spec §7) — wires skcomms.glossa to a MeshBus for N-way meshing.

Announces its capability descriptor; computes a GROUP density level = the minimum
over all heard peers (the weakest participant caps the room) via the pairwise
`negotiate`; sends level-tagged messages; receives, decodes, and exposes the
English gloss (the audit view)."""

from __future__ import annotations

import logging
from typing import Callable

from skcomms.glossa import gloss
from skcomms.glossa.codebook import Codebook
from skcomms.glossa.handshake import CapabilityDescriptor, negotiate
from skcomms.glossa.message import Message

from skchat.glossa_mesh import codec_ext as codec, protocol
from skchat.glossa_mesh.bus import MeshBus
from skchat.glossa_mesh.rate import RateController

logger = logging.getLogger(__name__)

MessageCb = Callable[[str, Message], None]  # (sender_fqid, message)


class GlossaMeshNode:
    def __init__(
        self,
        *,
        descriptor: CapabilityDescriptor,
        bus: MeshBus,
        codebook: Codebook,
        rate: RateController | None = None,
    ) -> None:
        self.descriptor = descriptor
        self.bus = bus
        self.codebook = codebook
        # Optional G2 rate controller. None => encode at the raw group ceiling
        # (fully backward-compatible with the pre-G2 fixed-tier behaviour).
        self.rate = rate
        self._peers: dict[str, CapabilityDescriptor] = {}
        self._on_message: MessageCb | None = None
        self.audit_log: list[str] = []
        bus.on_receive(self._on_frame)
        bus.on_leave(self.forget_peer)

    def on_message(self, cb: MessageCb) -> None:
        self._on_message = cb

    def forget_peer(self, src: str) -> None:
        """Drop a departed peer. group_level is a recomputed property, so removing
        the weakest peer un-caps the room instantly. Unknown src is a no-op."""
        self._peers.pop(src, None)

    async def start(self) -> None:
        await self.bus.start()

    async def stop(self) -> None:
        await self.bus.stop()

    @property
    def group_level(self) -> int:
        """Weakest-peer-caps: min over the pairwise negotiated level with each
        known peer. With no peers, fall back to our own max."""
        if not self._peers:
            return self.descriptor.max_level
        return min(negotiate(self.descriptor, p).level for p in self._peers.values())

    @property
    def effective_level(self) -> int:
        """The tier we actually encode at: the negotiated group ceiling, then
        rate-adapted down (or back up) by the RateController when one is set.
        Never exceeds group_level — the weakest peer still caps decodability."""
        ceiling = self.group_level
        if self.rate is None:
            return ceiling
        return self.rate.level(ceiling)

    def observe(self, quality: float) -> None:
        """Feed a [0,1] link-quality score to the rate controller (no-op if none
        is attached). Poor conditions degrade the tier; sustained good upgrade it."""
        if self.rate is not None:
            self.rate.observe(quality)

    async def announce(self) -> None:
        await self.bus.broadcast(protocol.frame_announce(self.descriptor))

    async def say(self, m: Message) -> None:
        level = self.effective_level
        body = codec.encode(m, level, self.codebook)
        self.audit_log.append(f"[tx L{level}] {gloss.to_english(m)}")
        await self.bus.broadcast(protocol.frame_message(level, body))

    def _on_frame(self, data: bytes, src: str) -> None:
        try:
            kind, payload = protocol.parse_frame(data)
        except ValueError:
            return
        if kind == protocol.ANNOUNCE:
            try:
                self._peers[src] = protocol.read_announce(payload)
            except Exception as exc:
                logger.warning(
                    "dropping malformed ANNOUNCE from %s (%s: %s)",
                    src,
                    type(exc).__name__,
                    exc,
                )
                return
        elif kind == protocol.MESSAGE:
            try:
                level, body = protocol.read_message(payload)
                m = codec.decode(body, level, self.codebook)
            except Exception as exc:
                logger.warning(
                    "dropping undecodable MESSAGE from %s (%s: %s)",
                    src,
                    type(exc).__name__,
                    exc,
                )
                return
            self.audit_log.append(f"[rx L{level}] {src}: {gloss.to_english(m)}")
            if self._on_message is not None:
                self._on_message(src, m)
