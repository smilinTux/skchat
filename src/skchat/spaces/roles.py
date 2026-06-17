"""Space roles → LiveKit grant flags (pure logic; tokens.py turns these into a JWT).

The speaker/listener switch is `can_publish`. Speakers are mic-only so no camera
or screen can be pushed into an audio room. Listeners are subscribe-only but keep
`can_publish_data` so they can raise hand / react / chat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


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
            can_publish_sources=["microphone"],
        )
    # LISTENER
    return RoleGrant(room=space_id, can_publish=False, can_publish_data=True)
