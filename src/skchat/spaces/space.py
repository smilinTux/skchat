"""Space model + deterministic Space id (mirrors call_session.derive_room)."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from enum import Enum


class SpaceStatus(str, Enum):
    OPEN = "open"      # created, not necessarily anyone live yet
    LIVE = "live"      # at least one speaker publishing
    ENDED = "ended"


def derive_space_id(host_fqid: str, slug: str) -> str:
    """Deterministic Space id from host FQID + slug.

    SHA-256 over "host_fqid/slug", base32 (lowercased, no padding), first 16
    chars → ~80 bits. Stable so a host can re-derive the same room for a slug.
    """
    digest = hashlib.sha256(f"{host_fqid.strip()}/{slug.strip()}".encode()).digest()
    b32 = base64.b32encode(digest).decode().lower().rstrip("=")
    return "space-" + b32[:16]


@dataclass
class Space:
    space_id: str
    host_fqid: str
    title: str
    slug: str
    status: SpaceStatus = SpaceStatus.OPEN
    speaker_cap: int = 10
    created_at: float = 0.0
    speakers: list[str] = field(default_factory=list)
    recording: bool = False
    egress_id: str = ""

    @property
    def room(self) -> str:
        """The LiveKit room name is the Space id (room auto-created on join)."""
        return self.space_id
