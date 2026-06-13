"""Mutual-consent raise-hand state machine + a thin LiveKit moderation wrapper.

The consent rule (spec §5): a listener goes on stage only when BOTH the host
invited them AND they raised their hand. `apply_action` is pure; `Moderator`
(Task 2) applies the result via LiveKit's update_participant.
"""

from __future__ import annotations

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
