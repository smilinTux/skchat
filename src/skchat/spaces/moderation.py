"""Mutual-consent raise-hand state machine + a thin LiveKit moderation wrapper.

The consent rule (spec §5): a listener goes on stage only when BOTH the host
invited them AND they raised their hand. `apply_action` is pure; `Moderator`
(Task 2) applies the result via LiveKit's update_participant.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

_ACTIONS = {"raise_hand", "lower_hand", "invite", "uninvite", "remove", "noop"}


@dataclass(eq=True)
class StageState:
    hand_raised: bool = False
    invited_to_stage: bool = False

    @property
    def on_stage(self) -> bool:
        return self.hand_raised and self.invited_to_stage


def parse_meta(metadata: str) -> StageState:
    if not metadata:
        return StageState()
    try:
        d = json.loads(metadata)
    except (json.JSONDecodeError, TypeError):
        return StageState()
    return StageState(
        hand_raised=bool(d.get("hand_raised", False)),
        invited_to_stage=bool(d.get("invited_to_stage", False)),
    )


def dump_meta(state: StageState) -> str:
    return json.dumps({"hand_raised": state.hand_raised,
                       "invited_to_stage": state.invited_to_stage})


def apply_action(state: StageState, action: str) -> tuple[StageState, bool]:
    """Return (new_state, can_publish). can_publish is the AND-gate: True only
    when both flags are set after the action."""
    if action not in _ACTIONS:
        raise ValueError(f"unknown stage action: {action!r}")
    s = StageState(state.hand_raised, state.invited_to_stage)
    if action == "raise_hand":
        s.hand_raised = True
    elif action == "lower_hand":
        s.hand_raised = False
    elif action == "invite":
        s.invited_to_stage = True
    elif action == "uninvite":
        s.invited_to_stage = False
    elif action == "remove":
        s.hand_raised = False
        s.invited_to_stage = False
    # "noop" leaves state unchanged
    return s, s.on_stage


# --- LiveKit moderation wrapper ---------------------------------------------

def _http_url(ws_url: str) -> str:
    return ws_url.replace("ws://", "http://").replace("wss://", "https://")


class Moderator:
    """Applies stage transitions + mute/kick via LiveKit's room service.

    `_room_service` is injectable for tests; in production it's built lazily from
    `api.LiveKitAPI(...).room`.
    """

    def __init__(self, ws_url: str, api_key: str, api_secret: str,
                 *, _room_service=None) -> None:
        self._ws_url = ws_url
        self._key = api_key
        self._secret = api_secret
        self._svc = _room_service
        self._client = None  # the LiveKitAPI instance (built lazily) to aclose()
        self._locks: dict[tuple[str, str], "asyncio.Lock"] = {}
        # refcount of in-flight + waiting users per lock key, so we only evict a
        # lock when nobody else still needs it (bounded-growth cleanup).
        self._lock_users: dict[tuple[str, str], int] = {}

    def _service(self):
        if self._svc is not None:
            return self._svc
        from livekit import api
        self._client = api.LiveKitAPI(_http_url(self._ws_url), self._key,
                                      self._secret)
        self._svc = self._client.room
        return self._svc

    async def aclose(self) -> None:
        """Close the cached LiveKit client, if one was built/injected. Safe to
        call when never built (no-op). Callers should invoke this on shutdown."""
        client = self._client if self._client is not None else self._svc
        if client is not None and hasattr(client, "aclose"):
            await client.aclose()

    async def stage_action(self, room: str, identity: str, action: str) -> bool:
        """Read current metadata, apply the consent action, push the new metadata
        + can_publish permission. Serialized per (room, identity) so concurrent
        raise_hand + invite cannot lose an update."""
        from livekit import api
        key = (room, identity)
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._lock_users[key] = self._lock_users.get(key, 0) + 1
        try:
            async with lock:
                svc = self._service()
                current = await svc.get_participant(
                    api.RoomParticipantIdentity(room=room, identity=identity))
                state = parse_meta(getattr(current, "metadata", "") or "")
                new_state, can_publish = apply_action(state, action)
                await svc.update_participant(api.UpdateParticipantRequest(
                    room=room, identity=identity, metadata=dump_meta(new_state),
                    permission=api.ParticipantPermission(
                        can_publish=can_publish, can_subscribe=True,
                        can_publish_data=True),
                ))
                return can_publish
        finally:
            # evict the lock once nobody else is holding or waiting for it, so the
            # dict does not grow unbounded across many distinct identities.
            self._lock_users[key] -= 1
            if self._lock_users[key] <= 0:
                self._lock_users.pop(key, None)
                self._locks.pop(key, None)

    async def kick(self, room: str, identity: str) -> None:
        from livekit import api
        await self._service().remove_participant(
            api.RoomParticipantIdentity(room=room, identity=identity))

    async def mute(self, room: str, identity: str, track_sid: str) -> None:
        from livekit import api
        await self._service().mute_published_track(api.MuteRoomTrackRequest(
            room=room, identity=identity, track_sid=track_sid, muted=True))
