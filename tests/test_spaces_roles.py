import pytest

from skchat.spaces.roles import Role, RoleGrant, grant_for


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
