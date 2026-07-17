from pathlib import Path


def _html():
    # Resolve relative to this test file, not the CWD — the repo convention is
    # to run pytest from ~ (avoids the skmemory namespace collision), where a
    # bare "src/..." relative path does not resolve.
    p = Path(__file__).resolve().parent.parent / "src" / "skchat" / "static" / "space.html"
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


def test_join_card_present_with_editable_prefilled_name():
    html = _html()
    assert 'id="joinCard"' in html
    assert 'id="nameInput"' in html
    assert 'id="join"' in html
    # editable, not readonly/disabled
    assert "readonly" not in html.split('id="nameInput"')[1].split(">")[0]
    assert "disabled" not in html.split('id="nameInput"')[1].split(">")[0]


def test_guest_alias_generated_and_remembered_in_localstorage():
    html = _html()
    assert "Guest-" in html
    assert "localStorage" in html
    assert "skchat.space.guestName" in html
    # no empty names: falls back to a fresh alias when the field is cleared
    assert "randomGuestAlias" in html


def test_share_button_present_with_navigator_share_and_clipboard_fallback():
    html = _html()
    assert 'id="share"' in html
    assert "navigator.share" in html
    assert "navigator.clipboard" in html
    assert "Link copied" in html
    assert '"/space/" + spaceId' in html
