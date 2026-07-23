"""Mint role-scoped LiveKit JWTs for a Space (mirrors livekit_routes._mint_token).

Two token families, one per role family in `roles.py`:

- `mint_space_token` — audio Space roles (HOST / SPEAKER / LISTENER).
- `mint_conf_token`  — conference video roles (PARTICIPANT / PRESENTER /
  SOVEREIGN / AGENT / GUEST_CONF). Routes grants through the single
  `conf_grant_for()` factory so a guest can never be over-granted admin.
"""

from __future__ import annotations

import os
from datetime import timedelta

from skchat.spaces.roles import ConfRole, Role, RoleGrant, conf_grant_for, grant_for


def _video_grants_from(g: RoleGrant):
    """Translate a `RoleGrant` into a `livekit.api.VideoGrants`.

    Soft-imports livekit; `room_record` is mapped through when the installed
    livekit-api exposes it (it does on 1.x). `room_destroy` has no dedicated
    LiveKit grant — destroying a room is gated by `room_admin`, so we never set
    admin for a grant whose RoleGrant.room_destroy is False *and* we assert below.
    """
    from livekit import api  # soft dep, local import

    grants = api.VideoGrants(
        room_join=g.room_join,
        room=g.room,
        can_publish=g.can_publish,
        can_subscribe=g.can_subscribe,
        can_publish_data=g.can_publish_data,
        can_publish_sources=g.can_publish_sources or None,
        room_admin=g.room_admin,
    )
    # Map room_record when supported (audio roles leave it False).
    if hasattr(grants, "room_record"):
        grants.room_record = g.room_record
    return grants


def _build_token(
    api_module, key: str, secret: str, identity: str, name: str, g: RoleGrant, ttl: int,
    metadata: str = "",
) -> str:
    grants = _video_grants_from(g)
    token = (
        api_module.AccessToken(key, secret)
        .with_identity(identity)
        .with_name(name)
        .with_grants(grants)
        .with_ttl(timedelta(seconds=ttl))
    )
    # Participant metadata is the unspoofable server->client channel for a
    # participant's capauth soul_fingerprint (M1b trust badges). Guests lack the
    # can_update_own_metadata grant, so only the trusted mint sets it.
    if metadata:
        token = token.with_metadata(metadata)
    return token.to_jwt()


def mint_space_token(
    identity: str,
    name: str,
    role: "Role | str",
    space_id: str,
    ttl: int,
    *,
    api_key: str | None = None,
    api_secret: str | None = None,
    metadata: str = "",
) -> str:
    """Build a participant JWT scoped to an *audio Space* `role` in `space_id`.

    Creds default to the same env vars livekit_routes uses; tests pass explicit
    dummy creds so no live SFU is required. Raises ImportError if livekit-api is
    not installed, ValueError on an unknown role. [metadata] (a JSON string) is
    embedded as participant metadata (M1b: carries the capauth soul_fingerprint).
    """
    from livekit import api  # soft dep, local import

    key = api_key or os.getenv("SKCHAT_LIVEKIT_API_KEY", "")
    secret = api_secret or os.getenv("SKCHAT_LIVEKIT_API_SECRET", "")
    g = grant_for(role, space_id)
    return _build_token(api, key, secret, identity, name, g, ttl, metadata=metadata)


def mint_conf_token(
    identity: str,
    name: str,
    role: "ConfRole | str",
    space_id: str,
    ttl: int,
    *,
    sovereign_admin: bool = False,
    api_key: str | None = None,
    api_secret: str | None = None,
    metadata: str = "",
) -> str:
    """Build a participant JWT scoped to a *conference video* `role` in `space_id`.

    Camera + mic + screenshare capable. Grants are produced by the single
    `conf_grant_for()` factory, which guarantees GUEST_CONF can never carry
    room_admin/room_record. Only SOVEREIGN may request room_admin and only when
    `sovereign_admin=True`.

    Raises ImportError if livekit-api is not installed, ValueError on an unknown
    conf role.
    """
    from livekit import api  # soft dep, local import

    key = api_key or os.getenv("SKCHAT_LIVEKIT_API_KEY", "")
    secret = api_secret or os.getenv("SKCHAT_LIVEKIT_API_SECRET", "")
    g = conf_grant_for(role, space_id, sovereign_admin=sovereign_admin)
    return _build_token(api, key, secret, identity, name, g, ttl, metadata=metadata)
