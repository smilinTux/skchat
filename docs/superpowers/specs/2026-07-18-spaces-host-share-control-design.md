# Spaces host-controlled sharing (who can publish video)

Status: APPROVED to build (operator, 2026-07-18, after the identity fix). Design.

## Goal

The host wants control over who can SHARE (screen/camera video), beyond the
default "any speaker can publish." A speaker should still be able to talk
(audio) but the host can disable or allow their video sharing individually.

## Mechanism

LiveKit participant permissions carry `canPublishSources` (a list of allowed
sources: microphone, camera, screen_share, screen_share_audio). That is the
precise lever: revoke the video sources from a speaker to stop them sharing
while leaving their mic.

## Design

Default policy: unchanged, a promoted speaker can publish everything (mic +
video). The host can then DISABLE sharing for a specific speaker (revoke camera
+ screen_share + screen_share_audio, keep microphone + data), and re-ALLOW it.
This keeps current behavior by default and adds host control on top. A Space-wide
"only host can share" policy is a possible later extension, out of scope for v1.

### Server (skchat)

- New host-only endpoint `POST /spaces/{id}/set-sharing` with body
  `{requester, identity, allow: bool}`. `_require_host`. Uses the Moderator /
  `update_participant` path to set the target's `permission.canPublishSources`:
  - `allow=false`: `[MICROPHONE]` (+ data/subscribe unchanged), i.e. can talk,
    cannot share video.
  - `allow=true`: full sources (MICROPHONE, CAMERA, SCREEN_SHARE,
    SCREEN_SHARE_AUDIO).
  - Preserve `can_publish` overall true (so audio still works) and canSubscribe.
- Returns `{ok, sharing: bool}`. Mirror the existing mute/kick moderation style
  and tests.

### App (skchat-app)

- `SpacesService.setSharing(id, {requester, identity, allow})` calling the endpoint.
- Host actions sheet (space_room_screen.dart, the per-speaker host sheet): add
  "Disable sharing" / "Allow sharing" toggle for a speaker (host-only), reflecting
  the target's current video-publish permission.
- Speaker's own "Go live" (the camera/screen chooser): gated on whether THEIR
  permission allows video sources. If the host disabled their sharing, Go live is
  hidden/disabled with a short note ("The host has turned off your sharing"); mic
  mute/unmute stays. This reads the local participant's canPublishSources (comes
  through the permission-updated event the app now binds, PERMFIX).

## Testing

- Server: set-sharing host-only; allow=false sets canPublishSources to mic-only
  (video revoked, mic kept, can_publish still true); allow=true restores; non-host
  requester 403; unknown identity handled.
- App: host sheet shows Disable/Allow per speaker; calls setSharing with allow
  false/true; a speaker whose video sources are revoked sees Go live
  disabled/hidden but keeps mute/unmute; re-allow restores Go live.

## Out of scope

Space-wide "only host shares" policy; per-source granularity in the UI (camera vs
screen separately); audio-publish revocation (a muted/removed speaker is the
existing path).

## Surfaces

- Server: `src/skchat/spaces/routes.py` (endpoint) + `moderation.py`
  (canPublishSources set) + tests.
- App: `lib/services/spaces_service.dart` (setSharing) +
  `lib/features/spaces/space_room_screen.dart` (host action + speaker Go-live
  gating) + tests.
