import pytest

from skchat.spaces.roles import (
    CONF_PUBLISH_SOURCES,
    ConfRole,
    Role,
    RoleGrant,
    conf_grant_for,
    grant_for,
)


def test_host_can_publish_and_is_admin():
    g = grant_for(Role.HOST, "space-x")
    assert isinstance(g, RoleGrant)
    assert g.room == "space-x"
    assert g.room_join is True
    assert g.can_publish is True
    assert g.can_subscribe is True
    assert g.can_publish_data is True
    assert g.room_admin is True


def test_speaker_is_mic_only_not_admin():
    g = grant_for(Role.SPEAKER, "space-x")
    assert g.can_publish is True
    assert g.can_publish_sources == ["microphone"]  # no camera/screen
    assert g.room_admin is False


def test_listener_is_subscribe_only_but_can_signal():
    g = grant_for(Role.LISTENER, "space-x")
    assert g.can_publish is False
    assert g.can_subscribe is True
    assert g.can_publish_data is True  # raise-hand / react / chat
    assert g.room_admin is False


def test_unknown_role_raises():
    with pytest.raises(ValueError):
        grant_for("emperor", "space-x")  # type: ignore[arg-type]


# ── Conference video roles ────────────────────────────────────────────────────

_SCREENSHARE_SOURCES = {"screen_share", "screen_share_audio"}


def test_conf_publish_sources_exact_enum_strings():
    # The verified LiveKit TrackSource strings (snake_case, lowercased).
    assert CONF_PUBLISH_SOURCES == [
        "camera",
        "microphone",
        "screen_share",
        "screen_share_audio",
    ]


@pytest.mark.parametrize(
    "role",
    [ConfRole.PARTICIPANT, ConfRole.PRESENTER, ConfRole.SOVEREIGN, ConfRole.GUEST_CONF],
)
def test_conf_video_roles_include_screenshare(role):
    g = conf_grant_for(role, "conf-1")
    assert isinstance(g, RoleGrant)
    assert g.can_publish is True
    assert g.can_subscribe is True
    assert g.can_publish_data is True
    assert "camera" in g.can_publish_sources
    assert "microphone" in g.can_publish_sources
    assert _SCREENSHARE_SOURCES.issubset(set(g.can_publish_sources))


def test_participant_and_presenter_are_not_admin():
    for role in (ConfRole.PARTICIPANT, ConfRole.PRESENTER):
        g = conf_grant_for(role, "conf-1")
        assert g.room_admin is False
        assert g.room_record is False
        assert g.room_destroy is False


def test_guest_conf_can_screenshare_but_never_room_control():
    g = conf_grant_for(ConfRole.GUEST_CONF, "conf-1")
    assert _SCREENSHARE_SOURCES.issubset(set(g.can_publish_sources))
    # The whole point: a conf guest may share screen but NEVER moderate/record/destroy.
    assert g.room_admin is False
    assert g.room_record is False
    assert g.room_destroy is False


def test_guest_conf_admin_request_is_ignored():
    # sovereign_admin only applies to SOVEREIGN; a guest can never be elevated.
    g = conf_grant_for(ConfRole.GUEST_CONF, "conf-1", sovereign_admin=True)
    assert g.room_admin is False
    assert g.room_record is False
    assert g.room_destroy is False


def test_agent_is_mic_and_data_only():
    g = conf_grant_for(ConfRole.AGENT, "conf-1")
    assert g.can_publish is True
    assert g.can_subscribe is True
    assert g.can_publish_data is True
    assert g.can_publish_sources == ["microphone"]  # no camera / screen
    assert g.room_admin is False


def test_sovereign_no_admin_by_default():
    g = conf_grant_for(ConfRole.SOVEREIGN, "conf-1")
    assert _SCREENSHARE_SOURCES.issubset(set(g.can_publish_sources))
    assert g.room_admin is False


def test_sovereign_admin_flag_grants_room_admin():
    g = conf_grant_for(ConfRole.SOVEREIGN, "conf-1", sovereign_admin=True)
    assert g.room_admin is True
    # room_record/destroy stay False — admin is the only thing the flag toggles.
    assert g.room_record is False
    assert g.room_destroy is False


def test_conf_grant_accepts_string_role():
    g = conf_grant_for("participant", "conf-1")
    assert "screen_share" in g.can_publish_sources


def test_unknown_conf_role_raises():
    with pytest.raises(ValueError):
        conf_grant_for("emperor", "conf-1")  # type: ignore[arg-type]


# ── Regression: audio roles unchanged ─────────────────────────────────────────


def test_audio_speaker_stays_mic_only_no_regression():
    g = grant_for(Role.SPEAKER, "space-x")
    assert g.can_publish_sources == ["microphone"]
    assert "screen_share" not in g.can_publish_sources
    assert "camera" not in g.can_publish_sources
    assert g.room_admin is False


def test_audio_host_unchanged():
    g = grant_for(Role.HOST, "space-x")
    assert g.room_admin is True
    assert g.can_publish_sources == []  # host publishes via can_publish, no source filter
