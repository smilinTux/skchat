# Runbook: HLS TV-cast (Sprints 1 + 2)

Turn a live call/Space screen-share into a plain **HLS stream with a reachable URL**
so it can play in any browser, VLC, a smart-TV browser, or be cast to a TV.

- **Sprint 1 (below): the HLS URL + egress control plane.** POST `/livekit/hls/start`
  turns a room into an HLS stream and hands back `{egress_id, hls_url}`; POST
  `/livekit/hls/stop` ends it; GET `/livekit/hls/status` lists active egresses.
- **Sprint 2 (see "In-app Cast to TV" below): the in-app cast experience.** A
  "Cast to TV" button on the call / conf / Space control bar auto-starts the egress,
  plays the stream in-app, and offers Chromecast, AirPlay, and open-on-TV. The phone
  keeps its live WebRTC mic + chat while the TV plays the separate HLS stream.

## In-app Cast to TV (Sprint 2)

The SKChat Flutter web client (served at `/app/`) drives all of this from one button;
no URL copy-paste needed for the common case.

**What the user does:**

1. In any live call, conference, or Space, tap **Cast to TV** in the control bar.
2. A sheet opens, auto-starts the HLS egress for THIS room (POST `/livekit/hls/start`),
   and shows a small live preview.
3. Pick a target:
   - **Chromecast**: opens the Google Cast device picker; on select, the hls_url loads
     on the Cast receiver (the default media receiver plays HLS). Chrome / Edge / Android.
   - **AirPlay**: shown only on Safari / iOS (the `<video>` carries the native AirPlay
     route). Taps open the system AirPlay picker.
   - **Open on TV**: copies the hls_url and tries to open it, the guaranteed fallback.
     Paste it into a smart-TV browser or VLC. Also works as Chrome's built-in
     "Cast this tab" if the buttons do not find a device.
4. **Keep casting, back to call** dismisses the sheet but leaves the TV playing.
   **Stop casting** ends the egress (POST `/livekit/hls/stop`).
5. Leaving the room (call / conf / Space) auto-stops any active cast.

**Honest UX:** the TV runs a few seconds behind the live call (HLS buffers segments).
The phone's WebRTC audio + chat stay live and low-latency; only the TV video is delayed.

**How it is built (Flutter web + JS interop):**

- App UI: `skchat-app/lib/features/calls/cast_sheet.dart` (the sheet + control-bar
  button + `activeCastSessionProvider` + `stopActiveCast`), `cast_service.dart`
  (start/stop HTTP), `cast_stage_web.dart` / `cast_stage_stub.dart` (the
  `HtmlElementView` platform view, conditional-imported).
- JS glue: `skchat-app/web/sk_cast.js` (vendored, loaded from `web/index.html`). It owns
  the `<video>`, lazy-loads **hls.js** from a CDN for non-Safari, uses Safari's native
  HLS + AirPlay, and drives the **Google Cast SDK (CAF)** device picker + `loadMedia`.
  The Cast SDK + hls.js are only fetched on first cast, so they cost nothing until used.
- The LiveKit connection is never torn down for a cast: the cast video is a SEPARATE HLS
  playback to the TV. The app keeps mic + chat.

Physical-device casting (a real Chromecast / AirPlay TV) must be tested on the operator's
own devices on the same network; the build has been verified to compile and deploy.

The rest of this runbook is the Sprint 1 control plane the button sits on top of.

## Architecture

```
  browser / VLC / smart-TV  ──HTTPS 443──▶  .158 Funnel (noroc2027.tail204f0c.ts.net)
                                                 │  /hls/<room>/<file>  (public media proxy)
                                                 ▼
                                       skchat-webui@lumina  (.158, :8765)
                                                 │  proxies to SKCHAT_HLS_ORIGIN
                                                 ▼
                                       sk-hls-http  nginx  (.41, tailnet :8099)
                                                 │  serves ~/.skchat/hls
   ┌─────────────────────────────────────────────┘
   │
  LiveKit SFU (.158, livekit-server 1.9.1, tailnet :7880)
   │  Twirp API (start/stop/list egress)      ▲
   │                                          │  redis coordinates SFU <-> egress
   ▼                                          │
  sk-livekit-egress (.41, livekit/egress:v1.10.0)
   │  headless Chrome + GStreamer, RoomComposite
   ▼  writes HLS segments
  ~/.skchat/hls/<room>/  (index.m3u8 + live.m3u8 + segment_*.ts)   on .41
```

### Where things run (and why)

- **SFU: .158** (`livekit-server.service`, tailnet `100.108.59.57:7880`). Config
  `~/.config/livekit/livekit.yaml`; API keys `skchat-lumina` / `skchat-opus` /
  `skchat-chef`.
- **Egress + Redis + segment store + nginx: .41** (tailnet `100.86.156.5`). Egress
  runs a headless Chrome + GStreamer (CPU heavy) and writes HLS segments to disk, so
  it lives on .41 (20 cores, 62 GB, hundreds of GB free), NOT on .158 (4 cores, disk
  near full). .158 would be crushed and fill up with segments.
- **Redis is the shared coordinator.** Both the SFU (.158) and egress (.41) must talk
  to the **same** redis. It runs on .41 bound to .41's tailnet IP
  (`sk-redis`, `redis:7-alpine`, `100.86.156.5:6379`) so both boxes reach it.
  `~/.config/livekit/livekit.yaml` on .158 has a matching `redis:` section pointing at
  `100.86.156.5:6379`.

### Containers on .41 (do not touch skcomms / the Flutter build box)

| Container | Image | Net | Role |
|-----------|-------|-----|------|
| `sk-redis` | `redis:7-alpine` | tailnet `100.86.156.5:6379` | SFU<->egress coordination |
| `sk-livekit-egress` | `livekit/egress:v1.10.0` | host | RoomComposite -> HLS segments |
| `sk-hls-http` | `nginx:alpine` | host, tailnet `:8099` | serves `~/.skchat/hls` |

`livekit/egress:v1.10.0` is the version compatible with `livekit-server 1.9.1`
(per the LiveKit egress compatibility matrix). Egress config on .41:
`~/.skchat/egress/config.yaml` (redis `100.86.156.5:6379`, `ws_url ws://100.108.59.57:7880`,
`api_key skchat-lumina`, `enable_chrome_sandbox: false`).

## Serving env (skchat-webui@lumina on .158)

Set in the webui EnvironmentFile (`~/.config/skchat/guest.env`), read at call time:

| Var | Value | Meaning |
|-----|-------|---------|
| `SKCHAT_LIVEKIT_API_URL` | `http://100.108.59.57:7880` | SFU Twirp API (drives egress) |
| `SKCHAT_HLS_ORIGIN` | `http://100.86.156.5:8099` | .41 nginx the public proxy fetches from |
| `SKCHAT_HLS_EGRESS_DIR` | `/out/hls` | container-side output dir (maps to `~/.skchat/hls`) |
| `SKCHAT_HLS_SEGMENT_DURATION` | `6` | segment length (seconds) |
| `SKCHAT_HLS_LAYOUT` | `grid` | RoomComposite layout |
| `SKCHAT_HLS_PUBLIC_BASE` | (unset) | public base for `hls_url`; falls back to `SKCHAT_FUNNEL_PUBLIC_URL` |
| `SKCHAT_FUNNEL_PUBLIC_URL` | `https://noroc2027.tail204f0c.ts.net` | the .158 Funnel |

## The hls_url

```
https://noroc2027.tail204f0c.ts.net/hls/<room>/index.m3u8
```

`index.m3u8` is the full playlist; `live.m3u8` is a bounded live window. Segments are
`segment_*.ts`. The `/hls/<room>/<file>` route is a **public** media proxy: it fetches
from the .41 nginx and returns the correct HLS content types
(`application/vnd.apple.mpegurl` for playlists, `video/mp2t` for segments) with
permissive CORS, so an off-tailnet cast receiver / TV can pull it over the Funnel on
443. Room names are sanitized and filenames whitelisted (no path traversal).

## Start / stop / status an HLS egress for a room

Control endpoints are gated exactly like `/livekit/token`: callable only from
loopback / the tailnet, or with a valid operator token (`SKCHAT_GUEST_OPERATOR_TOKEN`).
Run these on .158 (loopback), or add the operator token header off-box.

```bash
# START: begin a RoomComposite HLS egress for a room.
# Returns {egress_id, hls_url, playlist, status}.
curl -s -X POST http://localhost:8765/livekit/hls/start \
  -H 'content-type: application/json' \
  -d '{"room":"lumina-and-chef"}' | jq .

# -> hls_url = https://noroc2027.tail204f0c.ts.net/hls/lumina-and-chef/index.m3u8
#    Open it in a browser / VLC / TV browser, or cast the URL.

# STATUS: list active egresses.
curl -s http://localhost:8765/livekit/hls/status | jq .

# STOP: stop an egress by id (from start/status).
curl -s -X POST http://localhost:8765/livekit/hls/stop \
  -H 'content-type: application/json' \
  -d '{"egress_id":"EG_xxxxxxxx"}' | jq .
```

Notes:
- The room must have a live publisher (a screen-share). RoomComposite renders whatever
  is on the room stage.
- `index.m3u8` appears a few seconds after start (first segment must be written).
- Always **stop** the egress when done; a running egress keeps a headless Chrome +
  GStreamer alive on .41 and keeps writing segments.

## Retention (disk does not grow unbounded)

A systemd `--user` timer on .41 prunes old output every 10 minutes:

- Timer/service: `sk-hls-retention.timer` -> `sk-hls-retention.service`
  (`OnCalendar` every 10 min).
- Script: `~/.skchat/hls-http/hls-retention.sh`.
- Policy: delete `*.ts` / `*.m3u8` / `*.mp4` older than `SK_HLS_RETENTION_MIN=30`
  minutes, then remove now-empty room dirs. Safe for a live cast: a live stream keeps
  rewriting recent segments, only stale files are removed.

```bash
# On .41:
systemctl --user status sk-hls-retention.timer
systemctl --user start  sk-hls-retention.service   # prune now
journalctl --user -u sk-hls-retention.service -n 30
```

To keep a stream longer, raise `SK_HLS_RETENTION_MIN` in the service unit.

## Start / stop the plumbing

```bash
# On .41 (egress + redis + nginx). New containers, named clearly; do NOT touch
# skcomms or the Flutter build containers.
docker start  sk-redis sk-livekit-egress sk-hls-http
docker restart sk-livekit-egress          # after editing ~/.skchat/egress/config.yaml
docker stop   sk-livekit-egress           # stops all casting (segments stop being written)
docker logs -f sk-livekit-egress

# On .158 (serving leg). Reloads the webui routes/env.
systemctl --user restart skchat-webui@lumina.service
```

## Restart / rollback (SFU config change)

Changing `livekit.yaml` on .158 (the `redis:` section) and restarting
`livekit-server` briefly drops any active call. Do it while quiet.

```bash
# 1. Snapshot first (already done: livekit.yaml.bak-hls).
cp ~/.config/livekit/livekit.yaml ~/.config/livekit/livekit.yaml.bak-hls

# 2. Restart the SFU.
systemctl --user restart livekit-server.service

# 3. Verify it came back healthy:
curl -s -o /dev/null -w '%{http_code}\n' https://noroc2027.tail204f0c.ts.net/livekit-ws   # 200
curl -s -X POST http://localhost:8765/livekit/token \
  -H 'content-type: application/json' \
  -d '{"identity":"chef","room":"lumina-and-chef"}' | jq -r '.token' | head -c 20   # a JWT

# ROLLBACK (if unhealthy): restore the snapshot and restart.
cp ~/.config/livekit/livekit.yaml.bak-hls ~/.config/livekit/livekit.yaml
systemctl --user restart livekit-server.service
```

## Verifying end to end

```bash
# Serving leg (through the Funnel, off-tailnet reachable):
curl -s -o /dev/null -w 'hls index: %{http_code}\n' \
  https://noroc2027.tail204f0c.ts.net/hls/<room>/index.m3u8   # 200 while a stream is live

# Redis coordination (egress logs should show it connected to 100.86.156.5:6379):
docker logs sk-livekit-egress 2>&1 | grep -i redis
```

## Sprint 2 (shipped)

Sprint 2 added the **in-app Cast to TV button** in the SKChat client, so a tap casts the
stream to a TV. See "In-app Cast to TV (Sprint 2)" at the top of this runbook for the
user steps and the code map. Delivered:

- **Flutter app UI**: a Cast button on the call / conf / Space control bar that calls
  `/livekit/hls/start`, gets the `hls_url`, plays it in-app, and offers Chromecast,
  AirPlay, and open-on-TV (`cast_sheet.dart`, `cast_service.dart`, `cast_stage_web.dart`).
- **Receiver plumbing**: the Google Cast SDK (CAF) with the default media receiver
  (`CC1AD845`, plays HLS URLs directly), Safari native AirPlay, and hls.js for Chrome,
  all in the vendored `web/sk_cast.js`.
- **Lifecycle**: the egress auto-starts on the first cast (room-scoped single egress,
  reused if already running) and is stopped on "Stop casting" or when the user leaves the
  room. Sprint-1 retention + the room-empties backstop still bound disk on .41.

**Deploy note:** the Cast button is client-side; deploying it is just rebuilding the
Flutter web app (`flutter build web --release --base-href /app/` on .41) and copying
`build/web/` into `src/skchat/static/app/`. The webui serves those static files from
disk, so **no `skchat-webui@lumina` restart is needed** for an app-only build. The
Sprint-1 backend routes (`livekit_routes.py`) were unchanged by Sprint 2.
