"""Group A/V call support for the daemon API proxy (the WEB HTTP path).

Phase 3 of the Sovereign Comms Suite — "On-the-fly group video + screenshare"
(see docs/comms-suite-plan.md §5 Phase 3 + §1 "one room is one entity").

The Flutter app is a **web build** — its ``DaemonService`` CLI methods are
no-ops on web (``kIsWeb`` guard). So every group-call feature must work over the
same-origin webui HTTP endpoints. This module backs those endpoints with:

* the real :class:`skchat.group.GroupChat` membership from Phase 2
  (``daemon_proxy_groups`` — the SAME ``~/.skchat/groups/*.json`` store), and
* the LiveKit token mint from ``livekit_routes`` / ``call_session``.

Model (mirrors the 1:1 ``call_session.derive_room`` "single-token room"
philosophy, generalized from per-pair to per-group):

* A group's LiveKit room name is **deterministic** from the group id —
  ``derive_group_room(group_id)`` -> ``"gcall-" + <16 base32 chars>``. Every
  member computes the same room with zero negotiation; the group id itself is
  not leaked to the SFU's room logs (it is hashed).
* Starting/joining a group call mints a **per-member, room-scoped** LiveKit JWT
  (publish + subscribe, can_publish_data). Tokens are minted ONLY for proven
  group members — a non-member is refused (403). The token's LiveKit identity is
  the caller's own URI so "who is on" maps 1:1 to membership.
* The ring reuses the 1:1 ``CALL_INVITE`` sentinel over the signed skcomms
  mailbox: a ``call_session.build_invite_body`` is fanned out to every other
  member (each addressed to that member's fqid), so members get the SAME
  incoming-call surface they already poll (``GET /call/incoming``).

Recording seam (Phase 6 — NOT built here): a group call's room name is the
stable handle a future LiveKit Egress would attach to. ``group_call_context``
returns ``room`` + ``recording_hook`` (the documented attach point) so the
minutes module can wire ``RoomCompositeEgress`` against it without touching this
module. See ``RECORDING_SEAM`` below.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger("skchat.daemon_proxy.groupcall")

# Group-call rooms get their own prefix so they never collide with the 1:1
# ``call-`` rooms derived by ``call_session.derive_room``.
_GROUP_ROOM_PREFIX = "gcall-"
_GROUP_ROOM_SUFFIX_LEN = 16  # 16 base32 chars = 80 bits

# Token TTL for a group-call participant token (6h — same as the 1:1 path).
GROUP_CALL_TOKEN_TTL = 21600

# ── RECORDING SEAM (Phase 6) ──────────────────────────────────────────────── #
# Phase 6 (recording -> meeting-minutes) attaches a LiveKit Egress to the group
# call's deterministic room. This is the single documented hook: the minutes
# module calls ``group_call_context(...)`` (or ``derive_group_room``) to get the
# room name, then starts a ``RoomCompositeEgress`` / TrackEgress against it. No
# recording is started here — Phase 3 only exposes the attach point.
RECORDING_SEAM = (
    "Phase 6 minutes: start a LiveKit Egress against the room returned by "
    "derive_group_room(group_id); consent gate via spaces.consent.ConsentLedger."
)


def derive_group_room(group_id: str) -> str:
    """Return a stable, opaque LiveKit room name for a group call.

    Deterministic in the group id (every member derives the same room with no
    negotiation), and an opaque hash so the group id is not exposed in the SFU's
    room logs. Mirrors :func:`skchat.call_session.derive_room` but for a single
    group id (not a sorted pair).
    """
    import base64
    import hashlib

    gid = (group_id or "").strip()
    digest = hashlib.sha256(f"group\n{gid}".encode("utf-8")).digest()
    b32 = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    return _GROUP_ROOM_PREFIX + b32[:_GROUP_ROOM_SUFFIX_LEN]


def is_member(group, identity_uri: str) -> bool:
    """True if *identity_uri* is a member of *group* (the token gate)."""
    if group is None:
        return False
    try:
        return group.get_member(identity_uri) is not None
    except Exception:
        return False


def _display_name_for(uri: str) -> str:
    """Short display name from a URI (``capauth:lumina@...`` -> ``lumina``)."""
    return (uri or "").split(":")[-1].split("@")[0] or uri


def mint_member_token(
    group,
    identity_uri: str,
    room: str,
    *,
    ttl: int = GROUP_CALL_TOKEN_TTL,
    name: Optional[str] = None,
) -> str:
    """Mint a room-scoped LiveKit JWT for a proven group member.

    Raises:
        PermissionError: if *identity_uri* is not a member of *group*.
        ImportError: if livekit-api is not installed.

    The grant is publish + subscribe + can_publish_data scoped to *room* only
    (never room_admin) — exactly the 1:1 call grant from
    ``livekit_routes._mint_token``, so a group member can publish camera, mic,
    and a screen-share track but cannot administer the room.
    """
    if not is_member(group, identity_uri):
        raise PermissionError(f"{identity_uri} is not a member of group {group.id}")

    from .livekit_routes import _mint_token

    return _mint_token(
        identity_uri,
        name or _display_name_for(identity_uri),
        room,
        ttl,
    )


def group_call_context(
    group,
    caller_uri: str,
    livekit_url: str,
    *,
    ttl: int = GROUP_CALL_TOKEN_TTL,
) -> dict[str, Any]:
    """Build the start/join response for a group call.

    Returns the deterministic room, a per-member scoped token for *caller_uri*,
    the SFU url, the member roster, and the (Phase-6) recording hook. Raises
    ``PermissionError`` if the caller is not a member.
    """
    room = derive_group_room(group.id)
    token = mint_member_token(group, caller_uri, room, ttl=ttl)
    return {
        "ok": True,
        "group_id": group.id,
        "room": room,
        "identity": caller_uri,
        "token": token,
        "livekit_url": livekit_url,
        "ttl_seconds": ttl,
        "members": [
            {
                "identity_uri": m.identity_uri,
                "display_name": m.display_name or _display_name_for(m.identity_uri),
                "role": m.role.value,
                "participant_type": m.participant_type.value,
            }
            for m in group.members
        ],
        # Phase-6 attach point (documented seam — no egress started in Phase 3).
        "recording_hook": {"room": room, "note": RECORDING_SEAM},
    }


def ring_members(
    group,
    caller_uri: str,
    room: str,
    livekit_url: str,
    *,
    topic: str = "",
    send_invite=None,
    resolve_fqid=None,
) -> list[str]:
    """Ring every other member of *group* with a signed group CALL_INVITE.

    Reuses the 1:1 ``call_session`` invite envelope (same ``CALL_INVITE``
    subject the members already poll for at ``GET /call/incoming``), so a group
    ring shows up on the existing incoming-call surface. The invite body carries
    a ``group_id`` so the client can label it a group call + join the right room.

    Args:
        send_invite: callable ``(from_fqid, to_fqid, room, livekit_url, topic,
            group_id) -> None``. Injected for tests / DRY; defaults to the
            skcomms mailbox sender used by the 1:1 path.
        resolve_fqid: callable ``(uri) -> fqid`` for addressing the envelope to
            a member. Defaults to the identity-bridge resolver; pass-through on
            failure.

    Returns the list of member URIs that were rung (best-effort; a failure to
    ring one member never aborts the call).
    """
    sender = send_invite or _default_send_group_invite
    resolver = resolve_fqid or _default_resolve_fqid

    rung: list[str] = []
    for member in group.members:
        if member.identity_uri == caller_uri:
            continue
        try:
            to_fqid = resolver(member.identity_uri)
            sender(
                from_fqid=resolver(caller_uri),
                to_fqid=to_fqid,
                room=room,
                livekit_url=livekit_url,
                topic=topic or f"Group call: {group.name}",
                group_id=group.id,
            )
            rung.append(member.identity_uri)
        except Exception as exc:  # noqa: BLE001 — ring is best-effort
            logger.debug("group ring to %s failed: %s", member.identity_uri, exc)
    return rung


def _default_resolve_fqid(uri: str) -> str:
    """Best-effort URI -> fqid for envelope addressing (pass-through on failure)."""
    try:
        from .identity_bridge import resolve_peer_name

        # resolve_peer_name canonicalizes short names; already-qualified URIs
        # pass through unchanged.
        return resolve_peer_name(uri) or uri
    except Exception:
        return uri


def _default_send_group_invite(
    *, from_fqid: str, to_fqid: str, room: str, livekit_url: str, topic: str,
    group_id: str,
) -> None:
    """Send a signed group CALL_INVITE over the skcomms mailbox.

    Builds the SAME ``CALL_INVITE`` envelope the 1:1 path uses (so the existing
    ``GET /call/incoming`` poll surfaces it) but tags the JSON body with
    ``group_id`` so the client knows it is a group call.
    """
    import json

    from skcomms.mailbox import send_message

    from .call_session import CALL_INVITE_SUBJECT, build_invite_body

    body = build_invite_body(
        from_fqid=from_fqid,
        to_fqid=to_fqid,
        room=room,
        livekit_url=livekit_url,
        topic=topic,
    )
    # Decorate the body with the group id (additive; 1:1 parser ignores it).
    try:
        data = json.loads(body)
        data["group_id"] = group_id
        data["ts"] = data.get("ts") or int(time.time())
        body = json.dumps(data)
    except Exception:
        pass
    send_message(to_fqid, body, subject=CALL_INVITE_SUBJECT)
