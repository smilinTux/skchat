from pathlib import Path


def _html():
    p = Path("src/skchat/static/space.html")
    return p.read_text(encoding="utf-8")


def test_raise_hand_posts_to_endpoint():
    html = _html()
    assert "/raise-hand" in html


def test_permissions_changed_enables_mic():
    html = _html()
    assert "ParticipantPermissionsChanged" in html
    assert "setMicrophoneEnabled" in html


def test_host_controls_present():
    html = _html()
    # host control endpoints wired in the page
    assert "/invite" in html
    assert "/kick" in html


def test_metadata_changed_drives_render():
    assert "ParticipantMetadataChanged" in _html()
