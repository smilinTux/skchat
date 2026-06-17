import pytest

from skchat.spaces.moderation import StageState, apply_action, dump_meta, parse_meta


def test_default_state_is_off_stage():
    s = StageState()
    assert s.hand_raised is False
    assert s.invited_to_stage is False
    assert s.on_stage is False


def test_both_flags_required_for_stage():
    assert StageState(hand_raised=True, invited_to_stage=False).on_stage is False
    assert StageState(hand_raised=False, invited_to_stage=True).on_stage is False
    assert StageState(hand_raised=True, invited_to_stage=True).on_stage is True


def test_raise_hand_alone_does_not_publish():
    state, can_publish = apply_action(StageState(), "raise_hand")
    assert state.hand_raised is True
    assert can_publish is False  # host hasn't invited yet


def test_invite_then_already_raised_goes_live():
    raised, _ = apply_action(StageState(), "raise_hand")
    state, can_publish = apply_action(raised, "invite")
    assert state.on_stage is True
    assert can_publish is True  # mutual consent reached


def test_invite_first_then_raise_goes_live():
    invited, cp1 = apply_action(StageState(), "invite")
    assert cp1 is False  # user hasn't consented yet
    state, can_publish = apply_action(invited, "raise_hand")
    assert can_publish is True


def test_remove_resets_both_and_demotes():
    on, _ = apply_action(StageState(hand_raised=True, invited_to_stage=True), "noop")
    state, can_publish = apply_action(
        StageState(hand_raised=True, invited_to_stage=True), "remove"
    )
    assert state.hand_raised is False
    assert state.invited_to_stage is False
    assert can_publish is False


def test_lower_hand_and_uninvite():
    s1, _ = apply_action(StageState(hand_raised=True, invited_to_stage=True), "lower_hand")
    assert s1.hand_raised is False and s1.on_stage is False
    s2, _ = apply_action(StageState(hand_raised=True, invited_to_stage=True), "uninvite")
    assert s2.invited_to_stage is False and s2.on_stage is False


def test_meta_round_trip():
    s = StageState(hand_raised=True, invited_to_stage=False)
    assert parse_meta(dump_meta(s)) == s
    assert parse_meta("") == StageState()  # empty metadata → default
    assert parse_meta("not json") == StageState()


def test_unknown_action_raises():
    with pytest.raises(ValueError):
        apply_action(StageState(), "explode")
