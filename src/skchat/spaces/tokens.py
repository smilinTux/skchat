"""Mint role-scoped LiveKit JWTs for a Space (mirrors livekit_routes._mint_token)."""

from __future__ import annotations

import os
from datetime import timedelta

from skchat.spaces.roles import Role, grant_for


def mint_space_token(
    identity: str,
    name: str,
    role: "Role | str",
    space_id: str,
    ttl: int,
    *,
    api_key: str | None = None,
    api_secret: str | None = None,
) -> str:
    """Build a participant JWT scoped to `role` in `space_id`.

    Creds default to the same env vars livekit_routes uses; tests pass explicit
    dummy creds so no live SFU is required. Raises ImportError if livekit-api is
    not installed, ValueError on an unknown role.
    """
    from livekit import api  # soft dep, local import

    key = api_key or os.getenv("SKCHAT_LIVEKIT_API_KEY", "")
    secret = api_secret or os.getenv("SKCHAT_LIVEKIT_API_SECRET", "")
    g = grant_for(role, space_id)

    grants = api.VideoGrants(
        room_join=g.room_join,
        room=g.room,
        can_publish=g.can_publish,
        can_subscribe=g.can_subscribe,
        can_publish_data=g.can_publish_data,
        can_publish_sources=g.can_publish_sources or None,
        room_admin=g.room_admin,
    )
    token = (
        api.AccessToken(key, secret)
        .with_identity(identity)
        .with_name(name)
        .with_grants(grants)
        .with_ttl(timedelta(seconds=ttl))
    )
    return token.to_jwt()
