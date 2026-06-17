"""Signed federation discovery events (spec §7) as Nostr events.

NIP-53-aligned kinds: 30312 = Space state, 10312 = membership/presence; a custom
30078-style app-data kind for the focus descriptor. Only build/parse here; the
relay publish/query I/O lives in nostr_io.py (Task 6) behind an injectable seam.
"""

from __future__ import annotations

import json

from skchat.spaces.federation.focus import Membership

FOCUS_KIND = 30078  # app-specific: SFU focus descriptor
SPACE_KIND = 30312  # NIP-53 live room
MEMBERSHIP_KIND = 10312  # NIP-53 room presence/membership


def build_focus_descriptor(*, host_fqid: str, auth_url: str, sfu_ws_url: str) -> dict:
    return {
        "kind": FOCUS_KIND,
        "tags": [["d", "sk-lk-focus"], ["host", host_fqid]],
        "content": json.dumps(
            {
                "type": "livekit",
                "host_fqid": host_fqid,
                "auth_url": auth_url,
                "sfu_ws_url": sfu_ws_url,
            }
        ),
    }


def parse_focus_descriptor(ev: dict) -> dict:
    # M2: a hostile relay may serve non-JSON content; never let it crash parse.
    try:
        return json.loads(ev.get("content") or "{}")
    except (ValueError, TypeError):
        return {}


def build_space_state(*, space_id: str, title: str, host_fqid: str, status: str) -> dict:
    return {
        "kind": SPACE_KIND,
        "tags": [["d", space_id], ["title", title], ["host", host_fqid], ["status", status]],
        "content": "",
    }


def build_membership(*, fqid: str, space_id: str, foci_preferred: str, issued_at: int) -> dict:
    return {
        "kind": MEMBERSHIP_KIND,
        "tags": [
            ["a", f"{SPACE_KIND}:{space_id}"],
            ["fqid", fqid],
            ["foci_preferred", foci_preferred],
        ],
        "content": "",
        "created_at": issued_at,
    }


def parse_membership(ev: dict) -> Membership:
    # M2: harden against hostile/malformed relay events — tags may be None or
    # contain non-list / short entries, and created_at may be non-numeric.
    raw_tags = ev.get("tags") or []
    tags = {t[0]: t[1] for t in raw_tags if isinstance(t, list) and len(t) >= 2}
    try:
        issued_at = int(ev.get("created_at", 0))
    except (ValueError, TypeError):
        issued_at = 0
    return Membership(
        fqid=tags.get("fqid", ""),
        foci_preferred=tags.get("foci_preferred", ""),
        issued_at=issued_at,
    )
