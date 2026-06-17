"""GlossaMeshSession — a thin, synchronous encode/decode surface over the mesh
public API (spec §7), with the audit gloss surfaced on every call.

This ties a local CapabilityDescriptor + Codebook to the GROUP-level negotiation
rule (weakest-peer-caps): given the capability descriptors of the other mesh
participants, it computes the densest mutually-decodable tier and encodes at it.
Every encoded message is rendered to its English audit gloss (the oversight
invariant — spec §5); a frame that cannot be glossed never leaves this surface.

It is bus-agnostic on the hot path: `encode`/`decode` do not require a live bus,
so a REST route or an agent can round-trip glossa over HTTP and ALWAYS receive
the human-readable gloss. The mesh wire framing (protocol.frame_message) is reused
so frames are interchangeable with what GlossaMeshNode broadcasts on a Space data
channel.
"""

from __future__ import annotations

from skcomms.glossa import codec, gloss
from skcomms.glossa.codebook import Codebook, default_codebook
from skcomms.glossa.handshake import CapabilityDescriptor, negotiate
from skcomms.glossa.message import Message

from skchat.glossa_mesh import protocol


class GlossaMeshSession:
    """Group-level glossa encode/decode with an always-on audit gloss.

    Args:
        descriptor: this session's local capability descriptor.
        codebook: the shared semantic codebook (L2 needs matching versions).
        peers: optional capability descriptors of the other mesh participants.
            The encode level is min(pairwise-negotiated level) over all peers —
            the weakest participant caps the room. Empty → our own max_level.
    """

    def __init__(
        self,
        *,
        descriptor: CapabilityDescriptor,
        codebook: Codebook | None = None,
        peers: list[CapabilityDescriptor] | None = None,
    ) -> None:
        self.descriptor = descriptor
        self.codebook = codebook or default_codebook()
        self._peers: list[CapabilityDescriptor] = list(peers or [])
        self.audit_log: list[str] = []

    @property
    def group_level(self) -> int:
        """Weakest-peer-caps: min over the pairwise negotiated level with each
        known peer. With no peers, fall back to our own max."""
        if not self._peers:
            return self.descriptor.max_level
        return min(negotiate(self.descriptor, p).level for p in self._peers)

    def add_peer(self, peer: CapabilityDescriptor) -> None:
        self._peers.append(peer)

    def encode(self, m: Message) -> dict:
        """Encode a Message to a mesh wire frame at the negotiated group level and
        return the wire bytes, the English audit gloss, the tier, and the codebook
        version. The gloss is computed by re-decoding the produced frame, so the
        returned gloss is provably what the frame says (the audit invariant).
        """
        level = self.group_level
        body = codec.encode(m, level, self.codebook)
        wire = protocol.frame_message(level, body)
        # AUDIT INVARIANT: gloss from the wire, not the source — prove decodability.
        audit = self._gloss_wire(wire)
        self.audit_log.append(f"[tx L{level}] {audit}")
        return {
            "wire": wire,
            "gloss": audit,
            "tier": level,
            "lexicon_version": self.codebook.version,
        }

    def decode(self, wire: bytes) -> dict:
        """Decode a mesh wire frame (a MESSAGE frame) back to a Message and its
        English audit gloss. Raises ValueError on a non-message / malformed frame.
        """
        kind, payload = protocol.parse_frame(wire)
        if kind != protocol.MESSAGE:
            raise ValueError(f"not a glossa MESSAGE frame (kind={kind})")
        level, body = protocol.read_message(payload)
        m = codec.decode(body, level, self.codebook)
        audit = gloss.to_english(m)
        self.audit_log.append(f"[rx L{level}] {audit}")
        return {"message": m, "gloss": audit, "tier": level}

    def _gloss_wire(self, wire: bytes) -> str:
        """Decode a freshly-encoded MESSAGE frame to its English gloss. Used to
        enforce the audit invariant at encode time: if a frame cannot be glossed,
        encode() fails loudly rather than emitting un-auditable language."""
        _, payload = protocol.parse_frame(wire)
        level, body = protocol.read_message(payload)
        return gloss.to_english(codec.decode(body, level, self.codebook))
