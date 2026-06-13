"""Mesh wire framing (spec §7). type byte + payload. ANNOUNCE carries a JSON
capability descriptor; MESSAGE carries [level byte][codec bytes] so a receiver
decodes at the SENDER's level even if its own peer-view differs transiently.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from skcomms.glossa.handshake import CapabilityDescriptor

ANNOUNCE = 0
MESSAGE = 1


def frame_announce(d: CapabilityDescriptor) -> bytes:
    return bytes([ANNOUNCE]) + json.dumps(asdict(d)).encode()


def frame_message(level: int, body: bytes) -> bytes:
    return bytes([MESSAGE, level & 0xFF]) + body


def parse_frame(raw: bytes) -> tuple[int, bytes]:
    if not raw:
        raise ValueError("empty frame")
    return raw[0], raw[1:]


def read_announce(payload: bytes) -> CapabilityDescriptor:
    return CapabilityDescriptor(**json.loads(payload.decode()))


def read_message(payload: bytes) -> tuple[int, bytes]:
    if not payload:
        raise ValueError("empty message payload")
    return payload[0], payload[1:]
