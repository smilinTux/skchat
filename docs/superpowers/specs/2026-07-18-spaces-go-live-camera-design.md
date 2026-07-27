# Spaces "Go live" with camera (front/back), plus a source chooser

Status: APPROVED (operator, 2026-07-18). Ready for implementation.

## Problem

"Go live" in a Space publishes a screen share. Screen capture (`getDisplayMedia`)
is blocked on mobile browsers, so mobile users cannot go live at all (they get
the "screen sharing needs the desktop app" message). But camera capture
(`getUserMedia`) works fine on mobile Safari and Chrome. So a mobile user should
be able to go live with their camera.

## Decision (operator)

- "Go live" opens a CHOOSER on every platform: Camera (front) / Camera (back) /
  Screen. Screen is desktop-only (hidden or disabled on mobile via the existing
  `isMobileWeb` guard). Everyone can go live on camera; desktop can still screen-share.
- Default camera facing when going live: FRONT (selfie). Back is a one-tap choice
  in the chooser and a toggle while live.

## Design

The plumbing largely exists: `LiveKitCallService.setCameraEnabled` (livekit_call_service.dart:689)
and `switchCameraDevice` (:1451) already publish/switch the camera for the 1:1 and
conference call screens. Spaces just never used them.

- **Go live control** (shown for a speaker, `canPublish`): tapping it opens a small
  source chooser sheet in the Space room style:
  - Camera (front) [default emphasis]
  - Camera (back)
  - Screen share (desktop only; hidden/disabled on mobile with the existing note)
- **Camera publish:** on choosing a camera, publish it via the existing camera path
  with the selected facing (`CameraCaptureOptions` cameraPosition front=`user` /
  back=`environment`; front is the default). Reuse `setCameraEnabled`; do not add a
  parallel path.
- **Front/back while live:** a switch-camera control (reusing `switchCameraDevice` /
  the camera position flip) so a live speaker can flip front<->back without stopping.
- **Stop:** while any video is live, "Go live" becomes "Stop"; stopping tears down the
  active video source (camera or screen).
- **One active video source at a time:** camera XOR screen. Choosing one stops the
  other (mirror the existing system-audio mutual-exclusion pattern). Keeps the UI and
  the publish state simple; no simultaneous camera+screen in v1.
- **Video stage rendering:** the Space watch stage (`_WatchStage`,
  space_room_screen.dart:1053) currently renders only screen-share video. Generalize
  its video-source selection to also include camera publications
  (`TrackSource.camera`) so every participant sees a live camera, not just a screen.
  Fullscreen (fullscreen_video_stage.dart) works on whatever video the stage renders,
  so it comes along for free.
- **Mobile:** the chooser hides/disables Screen (existing `isMobileWebProvider` /
  screen-share guard) and shows the camera options. Camera `getUserMedia` permission
  is prompted by the browser; handle deny/no-camera with a plain non-blocking message
  (mirror the mic error handling).

## Testing

- Chooser shows Camera(front)/Camera(back) on all platforms; Screen only on
  non-mobile.
- Going live with Camera(front) publishes a camera track with front facing (default);
  Camera(back) publishes with back facing.
- Front/back toggle while live switches the camera without unpublishing.
- Stop tears down the active video source; choosing camera while screen is live (or
  vice versa on desktop) stops the previous source (mutual exclusion).
- `_WatchStage` renders a remote participant's camera track (not only SCREEN_SHARE).
- Mobile: Screen option absent; camera publish path reached; getUserMedia error shows
  a friendly message.

## Deploy (both artifacts, do not repeat the miss)

- Native: rebuild the Linux target for .41.
- WEB (the bundle mobile users load at the funnel `/app/`): `flutter build web
  --release --base-href /app/`, rsync into skchat `src/skchat/static/app/`, commit,
  restart the webui. The `--base-href /app/` is mandatory or the app will not load
  under the subpath.

## Out of scope

Simultaneous camera + screen; device selection beyond front/back; beauty/filters;
the standalone space.html page (the app under `/app/` is the mobile surface).
