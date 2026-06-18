"""Space roles → LiveKit grant flags (pure logic; tokens.py turns these into a JWT).

There are two parallel role families, deliberately kept separate so neither can
regress the other:

Audio Spaces (load-bearing — do NOT change semantics)
-----------------------------------------------------
The speaker/listener switch is `can_publish`. Speakers are mic-only so no camera
or screen can be pushed into an audio room. Listeners are subscribe-only but keep
`can_publish_data` so they can raise hand / react / chat.

Conference calls (video tier — `ConfRole` / `conf_grant_for`)
-------------------------------------------------------------
Conf calls need camera + mic + screenshare. Participants/presenters/sovereigns
get the full publish-source set; the AGENT role (Lumina-in-call) is mic + data
only; GUEST_CONF gets camera+mic+screenshare for an invited conf guest but is
NEVER granted room_admin / room_record / room_destroy. All conf grants flow
through the single `conf_grant_for()` factory so no caller can hand-roll a grant
that over-privileges a guest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# ── LiveKit TrackSource enum strings ──────────────────────────────────────────
# Verified against livekit-api 1.1.0: `can_publish_sources: Optional[List[str]]`
# is serialized verbatim into the JWT `video.canPublishSources` claim (via
# dataclasses.asdict in access_token.AccessToken.to_jwt). The server matches these
# against the TrackSource enum, lowercased snake_case:
#   livekit/protocol/models.pyi → MICROPHONE, CAMERA, SCREEN_SHARE, SCREEN_SHARE_AUDIO
# The existing audio SPEAKER already uses "microphone"; the screenshare strings
# are "screen_share" / "screen_share_audio".
SRC_MICROPHONE = "microphone"
SRC_CAMERA = "camera"
SRC_SCREEN_SHARE = "screen_share"
SRC_SCREEN_SHARE_AUDIO = "screen_share_audio"

# Full publish set for a conference video participant (cam + mic + screenshare).
CONF_PUBLISH_SOURCES: list[str] = [
    SRC_CAMERA,
    SRC_MICROPHONE,
    SRC_SCREEN_SHARE,
    SRC_SCREEN_SHARE_AUDIO,
]


# ── Audio Space roles (unchanged) ─────────────────────────────────────────────


class Role(str, Enum):
    HOST = "host"
    SPEAKER = "speaker"
    LISTENER = "listener"


@dataclass
class RoleGrant:
    room: str
    room_join: bool = True
    can_publish: bool = False
    can_subscribe: bool = True
    can_publish_data: bool = True
    can_publish_sources: list[str] = field(default_factory=list)
    room_admin: bool = False
    # Sensitive room-control grants. Audio roles never set these; they exist on the
    # dataclass so conf grants can carry an explicit False and the conf factory can
    # assert they are denied for guests.
    room_record: bool = False
    room_destroy: bool = False


def grant_for(role: "Role | str", space_id: str) -> RoleGrant:
    try:
        role = Role(role)
    except ValueError as exc:
        raise ValueError(f"unknown space role: {role!r}") from exc

    if role is Role.HOST:
        return RoleGrant(room=space_id, can_publish=True, can_publish_data=True, room_admin=True)
    if role is Role.SPEAKER:
        return RoleGrant(
            room=space_id,
            can_publish=True,
            can_publish_data=True,
            can_publish_sources=[SRC_MICROPHONE],
        )
    # LISTENER
    return RoleGrant(room=space_id, can_publish=False, can_publish_data=True)


# ── Conference video roles ────────────────────────────────────────────────────


class ConfRole(str, Enum):
    """Conference-call role tiers (camera + mic + screenshare capable)."""

    PARTICIPANT = "participant"  # standard attendee: full publish, no admin
    PRESENTER = "presenter"  # presenter: same grants as participant
    SOVEREIGN = "sovereign"  # host/owner: full publish, optionally room_admin
    AGENT = "agent"  # Lumina-in-call: mic + data only, no admin
    GUEST_CONF = "guest_conf"  # invited external guest: full publish, NEVER admin


# Roles that are guests / untrusted: must never receive room-control grants.
_CONF_GUEST_ROLES: frozenset[ConfRole] = frozenset({ConfRole.GUEST_CONF})


def conf_grant_for(
    role: "ConfRole | str",
    space_id: str,
    *,
    sovereign_admin: bool = False,
) -> RoleGrant:
    """Single grant factory for conference video roles.

    All conf-call token minting MUST route through here so no caller can
    hand-assemble a grant that accidentally over-privileges a guest. The factory
    structurally guarantees:

      - GUEST_CONF (and any future guest role) can NEVER carry room_admin /
        room_record / room_destroy — these are forced False after assembly.
      - Only SOVEREIGN may request room_admin, and only when `sovereign_admin`
        is explicitly True.
      - AGENT is mic + data + subscribe only (no camera/screen, no admin) so the
        in-call agent participant cannot push video or moderate.

    Args:
        role: a ConfRole (or its string value).
        space_id: the LiveKit room name.
        sovereign_admin: if True AND role is SOVEREIGN, grant room_admin.

    Returns:
        RoleGrant ready for tokens.mint_conf_token.

    Raises:
        ValueError: on an unknown conf role.
    """
    try:
        role = ConfRole(role)
    except ValueError as exc:
        raise ValueError(f"unknown conf role: {role!r}") from exc

    if role is ConfRole.AGENT:
        grant = RoleGrant(
            room=space_id,
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True,
            can_publish_sources=[SRC_MICROPHONE],
            room_admin=False,
        )
    elif role is ConfRole.SOVEREIGN:
        grant = RoleGrant(
            room=space_id,
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True,
            can_publish_sources=list(CONF_PUBLISH_SOURCES),
            room_admin=bool(sovereign_admin),
        )
    else:
        # PARTICIPANT / PRESENTER / GUEST_CONF: full publish, no admin.
        grant = RoleGrant(
            room=space_id,
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True,
            can_publish_sources=list(CONF_PUBLISH_SOURCES),
            room_admin=False,
        )

    # Hard guard: guests can never hold room-control grants, regardless of how
    # this factory is later edited. This is the single chokepoint.
    if role in _CONF_GUEST_ROLES:
        grant.room_admin = False
        grant.room_record = False
        grant.room_destroy = False

    return grant
