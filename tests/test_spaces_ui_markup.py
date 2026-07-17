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
    # exactly one raise-hand endpoint call in the whole page: the control-bar
    # hand button (via the handleHandClick dispatcher) and the banner's Join
    # stage button both funnel through the same raiseHand() fetch
    assert html.count("/raise-hand") == 1
    assert 'document.getElementById("hand").onclick = handleHandClick' in html
    assert 'document.getElementById("invitedJoin").onclick = raiseHand' in html
    assert "await raiseHand();" in html


def test_hand_button_relabels_to_join_stage_while_invited():
    html = _html()
    assert "Join stage" in html
    assert 'handBtn.textContent = "Join stage";' in html


def test_single_state_function_owns_control_bar_visibility():
    # Y1: one function is the sole owner of #hand/#mic/#leaveStage/banner
    # visibility, called on every state change, so they can never drift out
    # of sync (the operator bug: a promoted speaker saw a lit raise-hand
    # button AND a separate unmute button at the same time).
    html = _html()
    assert "function updateStageControls()" in html
    # the two old split functions are gone, not just renamed-and-kept
    assert "function updateMicButton(" not in html
    assert "function updateInvitedBanner(" not in html
    # called on connect, on both room events named in the brief, and after
    # every toggle (mic click, raiseHand, leaveStage, dismiss): count actual
    # call sites (trailing ";"), not the definition or the doc comment above it
    assert html.count("updateStageControls();") >= 6


def test_hand_hidden_when_speaker():
    # State 5: on stage, #hand is hidden entirely. A speaker never sees a
    # raise-hand button next to their mute control.
    html = _html()
    body = html.split("function updateStageControls()")[1].split("function micErrorMessage")[0]
    assert 'if (canPublish) {' in body
    assert 'handBtn.style.display = "none";' in body


def test_mic_promoted_to_primary_when_speaker():
    # State 5: #mic is the one prominent control, filled/primary, not the
    # muted ghost look, with an obvious mic-muted vs live/lit distinction.
    html = _html()
    body = html.split("function updateStageControls()")[1].split("function micErrorMessage")[0]
    assert 'micBtn.classList.remove("ghost");' in body
    assert 'micBtn.classList.add("primary");' in body
    assert 'micBtn.classList.toggle("live", micEnabled);' in body
    assert "button.primary.live" in html


def test_invited_join_stage_is_primary_not_ghost():
    # State 4: invited but not yet on stage, #hand becomes the primary
    # "Join stage" call to action (highlighted, not a ghost button).
    html = _html()
    body = html.split("function updateStageControls()")[1].split("function micErrorMessage")[0]
    assert 'handBtn.classList.remove("ghost", "live");' in body
    assert 'handBtn.classList.add("primary");' in body
    assert 'handBtn.textContent = "Join stage";' in body


def test_hand_raised_waiting_state_is_clearly_lit():
    # State 3: waiting on the host, the hand button shows a clearly RAISED
    # look and a tap lowers it again (self-service, no dedicated lower
    # endpoint exists server-side, so this reuses remove-from-stage which
    # already permits self, per routes.py's "host OR self" check).
    html = _html()
    assert "Hand raised (tap to lower)" in html
    assert "async function leaveStage()" in html
    assert "/remove-from-stage" in html


def test_leave_stage_button_present_and_only_shown_to_speakers():
    # State 5: a small secondary Leave stage control. Wired to the existing
    # remove-from-stage route (host-or-self), no new server route added.
    html = _html()
    assert 'id="leaveStage"' in html
    assert 'class="ghost" id="leaveStage" style="display:none"' in html
    body = html.split("function updateStageControls()")[1].split("function micErrorMessage")[0]
    assert 'leaveBtn.style.display = "inline-block";' in body
    assert 'leaveBtn.style.display = "none";' in body


def test_listener_never_sees_mic_and_speaker_never_sees_hand():
    html = _html()
    body = html.split("function updateStageControls()")[1].split("function micErrorMessage")[0]
    # canPublish branch (speaker): hand hidden, mic shown
    speaker_branch = body.split("if (canPublish) {")[1].split("return;")[0]
    assert 'handBtn.style.display = "none";' in speaker_branch
    assert 'micBtn.style.display = "inline-block";' in speaker_branch
    # non-speaker tail: mic hidden
    non_speaker_tail = body.split("if (canPublish) {")[1].split("return;", 1)[1]
    assert 'micBtn.style.display = "none";' in non_speaker_tail
