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


def test_storage_access_is_guarded_never_bare():
    # localStorage can throw (Safari private mode, storage-disabled policy,
    # sandboxed iframe); every touch must go through the guarded helpers so a
    # throw can't kill the script block at load or break the Join click.
    html = _html()
    assert "safeStorageGet" in html
    assert "safeStorageSet" in html
    # the only direct localStorage calls live inside the try blocks of the helpers
    assert html.count("localStorage.getItem") == 1
    assert html.count("localStorage.setItem") == 1
    assert "try { return localStorage.getItem" in html
    assert "try { localStorage.setItem" in html


def test_share_button_present_with_navigator_share_and_clipboard_fallback():
    html = _html()
    assert 'id="share"' in html
    assert "navigator.share" in html
    assert "navigator.clipboard" in html
    assert "Link copied" in html
    assert '"/space/" + spaceId' in html


def test_invited_banner_present_with_join_and_dismiss_hidden_by_default():
    html = _html()
    assert 'id="invitedBanner" style="display:none"' in html
    assert 'id="invitedJoin"' in html
    assert 'id="invitedDismiss"' in html
    assert "The host invited you to speak." in html


def test_invited_to_stage_parsed_and_gates_the_banner():
    html = _html()
    # invited_to_stage is now parsed on the client, not just hand_raised (was
    # the bug: the host-side ring only ever read hand_raised, so an invite with
    # no prior raised hand was invisible to the guest)
    assert "meta.invited_to_stage" in html
    assert "meta.hand_raised" in html
    assert "canPublish" in html
    # dismiss is latched until the flag transitions false -> true again
    assert "invitedDismissed" in html
    assert "wasInvited" in html


def test_invited_join_reuses_the_existing_raise_hand_fetch():
    html = _html()
    # exactly one raise-hand endpoint call in the whole page: both the
    # control-bar hand button and the banner's Join stage button share it
    assert html.count("/raise-hand") == 1
    assert 'document.getElementById("hand").onclick = raiseHand' in html
    assert 'document.getElementById("invitedJoin").onclick = raiseHand' in html


def test_hand_button_relabels_to_join_stage_while_invited():
    html = _html()
    assert "Join stage" in html
    assert '"Join stage" : "✋ Raise hand"' in html
