# Spaces: decouple content audio from the voice mic (independent mic while streaming)

Status: APPROVED to build (operator, 2026-07-19), execute after the DM-header fix lands.
Feasibility confirmed (scratchpad/task-DECOUPLE-report.md).

## Problem

Today the screen-share system audio and the voice mic are MUTUALLY EXCLUSIVE:
both publish under `TrackSource.microphone`, so `startScreenShareSystemAudio`
force-mutes the mic (`setMicEnabled(false)`), and the two cannot coexist. The
operator wants to stream content audio continuously while independently
muting/unmuting the mic (mute during content to avoid the acoustic echo, unmute
to talk with headphones). "The issue is not being able to mute the mic while the
content plays."

## Feasibility (confirmed)

livekit_client 2.5.0+hotfix.3 can publish our PulseAudio-monitor track as a
DISTINCT source, `TrackSource.screenShareAudio`, by constructing `LocalAudioTrack`
via its `@internal` constructor (`track/local/audio.dart:117-127`) wrapping the
monitor stream, then `publishAudioTrack()`, the exact pattern the SDK itself uses
in `createScreenShareTracksWithAudio`. Then `getTrackPublicationBySource` cleanly
disambiguates mic vs content audio, so the "two mic-source tracks collide" reason
for the mutual exclusion disappears, and no receiver change is needed (space.html
and the app attach audio by `kind`, not source).

Caveats: the constructor is `@internal` (analyzer lint only; runs fine with
`// ignore: invalid_use_of_internal_member`); could shift on a future SDK bump.
And `buildStreamId` gives screenShareVideo/screenShareAudio different stream-id
suffixes, so today's AVSYNC custom `stream: 'screenshare'` grouping is dropped in
favor of the SDK's documented server-side default pairing for screen_share +
screen_share_audio, which needs a real two-client lip-sync check.

## Design

Three INDEPENDENT, individually-toggleable media streams for a speaker:

1. **Content video** (screen or camera): Go live / Stop, as today.
2. **Content audio** (system audio): published as `TrackSource.screenShareAudio`,
   grouped with the screen video for lip-sync (SDK default pairing). Toggled with
   the existing "Share system audio" control; stays on independently of the mic.
3. **Voice mic** (`TrackSource.microphone`): the existing mute/unmute, now
   independent, NOT force-muted by content audio.

Changes:
- `startScreenShareSystemAudio`: build+publish the monitor track as
  `screenShareAudio` (internal ctor + ignore), grouped with the screen video.
  REMOVE the `setMicEnabled(false)` mutual-exclusion call.
- The mic-track lookups (`getTrackPublicationBySource(microphone)`,
  `_micTrackSidFor`) now unambiguously find the voice mic (content audio is a
  different source). Verify none assume a single audio track.
- Drop the AVSYNC custom `stream:` name on the screen-share publish now that
  screenShareVideo + screenShareAudio pair by the SDK default; VERIFY two-client
  lip-sync holds (E2E on .41 host + a guest).
- UX: when a speaker starts a content share WITH system audio, default the mic
  MUTED (avoid the accidental acoustic echo), with a one-line note that they can
  unmute to talk (headphones recommended on Linux). The mic mute/unmute control
  stays fully available and independent throughout the share.
- Play/stop: each stream is its own toggle, so the speaker can stop/start the
  video, toggle the content audio, and mute/unmute the mic independently (the
  operator's "stop or play video through the stream" plus independent mic).

## Testing

- Unit: publishing content audio as screenShareAudio while the mic is a separate
  microphone track; the two coexist; `getTrackPublicationBySource(microphone)`
  returns the VOICE mic, not the content track; muting/unmuting the mic does not
  touch the content-audio track and vice versa; default-mic-muted on content-share
  start.
- No mutual-exclusion force-mute remains.
- E2E (.41 host + guest, do after merge): content video + content audio stay
  lip-synced (drop-the-stream-hack verification), the mic mutes/unmutes
  independently while content keeps playing, no acoustic echo when mic is muted.

## Rollout

- Native + web build+deploy BOTH artifacts (web `--base-href /app/`, verify /app/
  render), as standard now.
- The `@internal` ctor risk is documented; if a future SDK bump breaks it, the
  fallback is the current mutual-exclusion behavior.

## Out of scope

Pausing the underlying OS media source (that is the user's media player, not the
stream); per-track volume mixing.
