# Runbook: HLS TV-cast (Sprint 1)

Turn a live call/Space screen-share into a plain **HLS stream with a reachable URL**
so it can play in any browser, VLC, a smart-TV browser, or be cast as a URL.

This is **Sprint 1: the HLS URL only**. Sprint 2 (separate) adds the in-app
Chromecast / AirPlay buttons. Until then, the returned `hls_url` is the whole
deliverable: paste it into a browser / VLC / a TV browser, or cast the URL.

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

## Sprint 2 (not in this sprint)

Sprint 2 adds the **in-app Chromecast / AirPlay buttons** in the SKChat client, so a
tap casts the stream to a TV. It still needs:

- Flutter app UI: a Cast button on the call/Space screen that calls
  `/livekit/hls/start`, gets the `hls_url`, and hands it to a Cast / AirPlay sender
  (Cast SDK receiver app + AirPlay `AVPlayer` URL).
- Receiver plumbing: a Cast receiver app id (default media receiver plays HLS URLs
  directly), and iOS AirPlay routing.
- Lifecycle: auto-start the egress when a viewer casts, auto-stop when the last viewer
  disconnects (so we do not leave a headless-Chrome egress running on .41).

For now (Sprint 1) the `hls_url` is the deliverable: it plays in any browser, VLC, a
smart-TV browser, or can be cast as a URL.
