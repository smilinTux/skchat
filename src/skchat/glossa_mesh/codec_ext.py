"""Tier dispatch for the mesh (G2): the L0-L3 codec surface.

skcomms.glossa.codec owns the G1 ladder (L0 English / L1 CBOR / L2 codebook).
G2 adds L3 (token-stream) WITHOUT touching skcomms: this wrapper routes L3 to
``skchat.glossa_mesh.tokenstream`` and delegates every other level to the skcomms
codec verbatim. It re-exports the level constants (now including L3) so mesh code
can ``from skchat.glossa_mesh import codec_ext as codec`` and keep one call site.
"""

from __future__ import annotations

from skcomms.glossa import codec as _codec
from skcomms.glossa.codebook import Codebook
from skcomms.glossa.message import Message

from skchat.glossa_mesh import tokenstream

# Re-exported ladder — L0-L2 from skcomms, L3 from tokenstream.
L0_ENGLISH = _codec.L0_ENGLISH
L1_SCHEMA = _codec.L1_SCHEMA
L2_CODEBOOK = _codec.L2_CODEBOOK
L3_TOKENSTREAM = tokenstream.L3_TOKENSTREAM

MAX_LEVEL = L3_TOKENSTREAM


def encode(m: Message, level: int, codebook: Codebook | None = None) -> bytes:
    if level == L3_TOKENSTREAM:
        return tokenstream.encode_l3(m, codebook)
    return _codec.encode(m, level, codebook)


def decode(raw: bytes, level: int, codebook: Codebook | None = None) -> Message:
    if level == L3_TOKENSTREAM:
        return tokenstream.decode_l3(raw, codebook)
    return _codec.decode(raw, level, codebook)
