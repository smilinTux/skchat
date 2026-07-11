# Runbook: Stream Kodi (or any desktop app) to a few people via SKChat

Goal: Chef plays something in Kodi on his Linux box and streams the picture AND the
sound to a handful of viewers, through an SKChat call. No OBS, no third-party
streaming service. It rides the normal SKChat / LiveKit call as a screen-share
video track plus an audio track.

## The one thing you have to understand first: tab audio vs desktop audio

Browsers capture screen-share audio very differently depending on WHAT you share:

- Sharing a browser TAB (e.g. a YouTube tab, a web player): Chromium captures that
  tab's audio for you automatically. In the Chrome share picker, pick the "Chrome
  Tab" surface and tick "Share tab audio". Nothing else to do. Viewers hear it.
- Sharing a DESKTOP WINDOW or the WHOLE SCREEN (e.g. the Kodi app): on Linux,
  `getDisplayMedia` almost never hands the browser any system audio. You get video
  only. So for Kodi you route Kodi's sound in as a normal microphone track, using a
  PulseAudio / PipeWire "Monitor of ..." source. That is what the rest of this
  runbook sets up.

Both paths end the same way for viewers: they just hear the audio track that got
published. The screen-share tile is auto-promoted to the big "stage" tile.

## Quick path (share a browser tab, zero setup)

If whatever you want to show can live in a browser tab, do this and skip the
PulseAudio section entirely:

1. Start a call in the SKChat app (or open the web call page at
   `https://<host>/livekit/<room>`).
2. Click "Add people". Copy the invite link and send it to your viewers. One link
   admits several guests into the same room.
3. Click "Share" (app) or "Screen" (web). In Chrome's picker choose "Chrome Tab",
   select the tab, and tick "Share tab audio". Done. Viewers see and hear it.

## Kodi path (desktop app, route audio via a monitor source)

Kodi is a native app, not a tab, so we feed its audio in as a "microphone" that is
actually the monitor of the speaker output.

### Step 1: confirm your audio server and find the sink Kodi plays to

Works the same on PulseAudio and PipeWire (PipeWire ships `pactl`).

```bash
# Which sinks (outputs) exist? Note the one your speakers/headphones use.
pactl list short sinks
# Example row:  47  alsa_output.pci-0000_00_1f.3.analog-stereo  ...  RUNNING
```

Every sink automatically has a matching monitor source named `<sink>.monitor`,
described as "Monitor of <sink>". List them:

```bash
pactl list short sources | grep -i monitor
# e.g.  alsa_output.pci-0000_00_1f.3.analog-stereo.monitor
```

If a monitor source shows up here, you are ready. It captures exactly what is
playing out of that sink, which is Kodi's audio while Kodi is playing.

### Step 2 (optional but recommended): a dedicated stream sink

Streaming the monitor of your main speakers also streams every other system sound
(notifications, other apps). To stream ONLY Kodi and still hear it locally, make a
dedicated null sink, send Kodi to it, and loop it back to your real speakers:

```bash
# Create a virtual sink named SKStream (remember the printed module id to unload later)
pactl load-module module-null-sink sink_name=skstream \
  sink_properties=device.description=SKStream

# Hear it yourself too: loop SKStream's monitor back to your real output
pactl load-module module-loopback source=skstream.monitor
```

Then point Kodi at it: Kodi > Settings > System > Audio > Audio output device,
choose "SKStream" (PulseAudio/PipeWire). Or move Kodi live without touching Kodi's
settings:

```bash
pactl list short sink-inputs          # find Kodi's stream id (left column)
pactl move-sink-input <kodi_id> skstream
```

Now `Monitor of SKStream` carries only Kodi.

To tear it all down afterwards:

```bash
pactl list short modules              # find the module ids you loaded
pactl unload-module <loopback_module_id>
pactl unload-module <null_sink_module_id>
```

### Step 3: start the call and share the screen

1. In the SKChat app, start a call (or open `https://<host>/livekit/<room>` in
   Chrome). Grant mic permission once so the browser will show device labels.
2. Click "Add people", copy the invite link, send it to your viewers. One link,
   several guests, same room.
3. Click "Share" (app) or "Screen" (web) and pick the Kodi window or the whole
   screen. Video starts streaming. There is no desktop audio on this track yet;
   that is expected on Linux.

### Step 4: select the monitor source as your microphone (this is the audio)

This is the step that actually streams Kodi's sound.

- App: open the microphone device picker in the call controls and choose the
  entry named "Monitor of <sink>" (or "Monitor of SKStream" if you made the
  dedicated sink).
- Web page (`/livekit`): use the mic dropdown in the toolbar and pick the same
  "Monitor of ..." entry.

SKChat deliberately does NOT hide monitor sources from the picker (it filters out
dead virtual cams like DroidCam/OBS, but a "Monitor of ..." source is always kept,
because it is exactly how you stream desktop audio). Once selected, whatever Kodi
plays is published as your audio track and every viewer hears it in sync with the
screen video.

Note: you are trading away your real mic while the monitor source is your input, so
your voice will not go out during the stream unless you switch the mic back. If you
want to narrate AND stream Kodi audio at once, use the dedicated SKStream sink and
add a second mic path, or just switch the picker back to your mic when you want to
talk.

## Verifying it works

- The publisher's log (web page log panel) should print:
  `published video screen_share` then `published audio ...`.
- A viewer should see the shared screen on the big stage tile and hear the audio.
- Under the hood: the screen video publishes as source `screen_share`; tab audio
  publishes as `screen_share_audio`; the Kodi monitor audio publishes as the normal
  `microphone` source. All are auto-subscribed by viewers.

This was verified headless with two Chrome clients (CDP): the publisher published
both `screen_share` (video) and `screen_share_audio` (audio) and a second client
subscribed to both, alongside the microphone track. See
`scripts/qa_two_browser.py` for the data-channel sibling test.

## Watch party in a Space (the easy path for a few people)

A Space is the nicer surface for this: the shared screen becomes the big main
stage automatically and everyone else is a listener around it, with chat and
reactions. Use this for a UFC night or any "a few friends watch one screen"
event. It rides the same LiveKit screen-share (video + audio) as above.

1. Create a Space in the SKChat app (Spaces tab, new Space). You are the host.
2. Add people / share the link. Invite from the app, or copy the guest link and
   send it to your viewers. They open it and land in the Space as listeners.
3. Go live. As host, tap "Go live" in the control bar (a speaker you promoted can
   too). That runs the same screen-share as a call: pick your Kodi window / the
   whole screen (or a browser tab if the fight is in a tab).
4. Listeners auto-see the stage. The moment you go live, the shared screen jumps
   to the big 16:9 stage at the top of the Space for every listener, labelled
   "Streaming: <you>", with the speaker rings, chat, and reactions below it. No
   one has to open a panel or click anything.
5. Linux desktop audio (Kodi window / whole screen). Same rule as the call path:
   `getDisplayMedia` will not hand you Kodi's system sound on Linux, so route it
   in as your mic via a PulseAudio / PipeWire "Monitor of ..." source (see the
   Kodi steps above). If the fight is in a browser tab, tick "Share tab audio"
   instead and you can skip the monitor source. Tap "Stop" to end the stream; the
   stage disappears and the Space falls back to the normal audio room.

This was verified headless with two Chrome clients (CDP): a listener subscribed
to the host's `screen_share` (video) plus `screen_share_audio` and `microphone`
tracks, which is exactly what the stage renders, so the listener sees and hears
the shared screen.

## Troubleshooting

- No "Monitor of ..." in the picker: the source may be suspended. Play something in
  Kodi first (a suspended monitor can hide), then reopen the picker. On some setups
  run `pactl list short sources | grep monitor` to confirm it exists, and toggle
  `pactl suspend-source <name> 0`.
- Viewers see video but hear nothing: you shared a window/screen (not a tab) and did
  NOT select a monitor source as the mic. Do Step 4.
- Tab audio missing: you forgot to tick "Share tab audio" in Chrome's picker, or you
  shared a window instead of a tab.
- Echo / robotic audio: the browser applies voice DSP to mic tracks. The web page
  already disables echo cancellation / noise suppression / auto gain for the screen
  path; if you still hear it on the mic path, disable those in the OS for that source.
- Firefox does not expose per-tab audio the way Chromium does. Use Chrome/Chromium
  for the tab-audio path; the monitor-source path works on any browser.
