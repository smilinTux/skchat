"""Deterministic per-pair call room + CALL_INVITE envelope helpers.

A call room is derived purely from the two participants' capauth FQIDs, so both
sides compute the same room with zero negotiation. The room name is an opaque
hash (FQIDs are not leaked to the LiveKit server's room logs).
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid


def derive_room(fqid_a: str, fqid_b: str) -> str:
    """Return a stable, order-independent room name for a pair of FQIDs.

    Args:
        fqid_a: one participant's capauth FQID (e.g. ``lumina@chef.skworld``).
        fqid_b: the other participant's FQID.

    Returns:
        ``"call-" + <16 lowercase base32 chars>`` — identical regardless of
        argument order.
    """
    joined = "\n".join(sorted([fqid_a.strip(), fqid_b.strip()]))
    digest = hashlib.sha256(joined.encode("utf-8")).digest()
    b32 = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    return "call-" + b32[:16]


CALL_INVITE_SUBJECT = "CALL_INVITE"
CALL_ACCEPT_SUBJECT = "CALL_ACCEPT"
CALL_DECLINE_SUBJECT = "CALL_DECLINE"


def build_invite_body(
    *, from_fqid: str, to_fqid: str, room: str, livekit_url: str
) -> str:
    """Serialize a CALL_INVITE control payload (JSON string) for skcomms."""
    return json.dumps(
        {
            "type": CALL_INVITE_SUBJECT,
            "from_fqid": from_fqid,
            "to_fqid": to_fqid,
            "room": room,
            "transport": "livekit",
            "livekit_url": livekit_url,
            "ts": int(time.time()),
            "nonce": uuid.uuid4().hex,
        }
    )


def parse_invite_body(body: str) -> dict:
    """Parse + validate a CALL_INVITE payload. Raises ValueError if not one."""
    data = json.loads(body)
    if data.get("type") != CALL_INVITE_SUBJECT:
        raise ValueError(f"not a CALL_INVITE: type={data.get('type')!r}")
    return data
