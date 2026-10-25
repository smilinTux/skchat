# LiveKit SFU 1.9.1 to 1.13.11 with WARP (2026-10-25)

Ops note for the noroc2027 (.158) SFU that carries Lumina's calls: FaceTime-style
rooms, the 1:1 call answerer, and the `skchat-lumina-call` voice agent.
Background research: `skcall/docs/research/warp-2026-10-25.md`.

## What changed

| Item | Before | After |
|------|--------|-------|
| `~/.local/bin/livekit-server` | 1.9.1 | **1.13.11** (sha256 checked against the release `checksums.txt`) |
| `~/.config/livekit/livekit.yaml` | no WARP | `rtc.enable_warp: true` added, every other key unchanged |
| systemd unit `livekit-server.service` + drop-ins | unchanged | unchanged |
| `~/.skenv` Python (`livekit` 1.1.5, `livekit-api` 1.1.0, `livekit-agents` 1.5.6) | unchanged | **unchanged** (see "Python SDKs" below) |
| `~/.skenv-call` Python (`livekit` 1.1.20, used by `skchat-lumina-call`) | unchanged | unchanged, already WARP-capable |

Only `livekit-server` was restarted. The Lumina voice agent resumed on its own
about 0.4 s after the new server came up (it is now on the `/rtc/v1` signal path,
which 1.9.1 did not have).

Note: a graceful stop of the old server waits for participants to leave. The idle
Lumina agent never leaves, so systemd hit its 90 s stop timeout and SIGKILLed 1.9.1.
Expect a ~90 s gap on any restart while the agent is in `lumina-and-chef`; a
`systemctl --user kill -s SIGKILL livekit-server` before the restart avoids the wait.

## WARP

- Config key: `rtc.enable_warp` (`pkg/config/config.go`, `RTCConfig.EnableWarp`).
  Server-wide, off by default, experimental. It enables SPED (DTLS inside STUN,
  in-band, no SDP marker) and SNAP (SCTP INIT in SDP, `a=sctp-init`). DTLS is still 1.2.
- It is negotiated per client. A client without WARP gets classic DTLS + SCTP, so
  browsers and old SDKs keep working.
- Client support:
  - Python `livekit` (rtc) **>= 1.1.17** (first build on `livekit-ffi` 0.12.76, which
    contains rust-sdks PR #1342 "Option for webrtc WARP", on by default and gated by
    the server). Our 1.1.20 offers `goog-sped-v1` and `a=sctp-init`.
  - Browsers: no SPED; SNAP only behind a Chrome origin trial. The webui pages
    (`livekit-client@2.6.0`) fall back, no change needed.
- Evidence it is on: the production config has the key, and a side instance of the
  same 1.13.11 binary and config at debug level logged `enable warp` for every
  transport and put `a=sctp-init` in its SDP answers only when the key was true
  (0 answers with it when false, 172 when true). Production runs at `info`, which
  does not log per-transport WARP.

## Measurements

Probe: two throwaway participants in a private `warp-probe-*` room on the SFU.
B times `connect()`, then the first data packet and the first subscribed audio
track from A. n = 30 joins each, medians in ms. Same host as the SFU, so RTT is
near zero and WARP's saving (1 to 3 RTT) is close to invisible here; it pays off
over WAN or lossy links (off-tailnet guests via TURN).

| Server | Client SDK | connect | first data | first track |
|--------|-----------|--------:|-----------:|------------:|
| 1.9.1 (before) | py `livekit` 1.1.20 | 327 | 335 | 340 |
| 1.9.1 (before) | py `livekit` 1.1.5 | 230 | 235 | 237 |
| 1.13.11 + WARP (after) | py `livekit` 1.1.20 | 196 | 200 | 201 |
| 1.13.11 + WARP (after) | py `livekit` 1.1.5 | 188 | 189 | 213 |

Controlled A/B on the same 1.13.11 binary (side instance, loopback):
WARP off 184 / 189 / 187, WARP on 157 / 164 / 164 for the 1.1.20 client; the
1.1.5 client (no WARP) was flat at 174 to 180. Spread is wide (min ~60, max ~600)
because the host runs near full CPU, so treat the WARP delta as indicative only.
Most of the before/after gain comes from the server upgrade itself.

## Python SDKs: why `~/.skenv` stays on livekit 1.1.5

Every `livekit-agents` release that pins a WARP-capable `livekit` (1.8.0 and up)
hard-depends on `livekit-local-inference`, whose only x86_64 wheels are built with
AVX and which has no sdist. noroc2027 is a QEMU VM whose CPU exposes SSE4.2 but no
AVX, so `import livekit.agents` dies with SIGILL (exit 132), not a catchable error.
The upgrade was applied, failed that import check, and was rolled back to a
byte-identical `pip freeze` (snapshot
`~/.skenv-snapshots/freeze-pre-livekit-warp-20261025-161040.txt`). This is most
likely why the 2026-10-06 attempt (`freeze-pre-livekit-1.8.5-*`) ended with the
separate `~/.skenv-call` venv instead.

What that means today:

- `skchat-lumina-call` (the voice agent, `~/.skenv-call`, livekit 1.1.20) gets WARP now.
- The call answerer and webui token minting (`~/.skenv`, livekit 1.1.5) work on the
  new server without WARP. Verified: the answerer's own `_join_and_publish` path,
  with a token from `/livekit/token`, joined and published in a private probe room.
- `pyproject.toml` keeps `livekit>=1.0`. Raising the floor to 1.1.17 would make
  `pip install -e` in `~/.skenv` fight `livekit-agents` 1.5.6 (`livekit==1.1.5`).

Options to get WARP on the answerer too (not done, needs a decision):
1. Run `skchat-call-answerer@` from `~/.skenv-call` with a drop-in like
   `skchat-lumina-call.service.d/zz-skenv-call.conf` (it already has skchat installed).
2. Or drop `livekit-agents` / `livekit-plugins-silero` from `~/.skenv` (only
   `scripts/lumina-livekit-agent.py` imports them and no unit runs it), then upgrade
   `livekit` + `livekit-api` there.

## Other compatibility notes (1.9.1 to 1.13.11)

- TURN auth/permission changes in 1.12.0 and 1.13.1 apply to the embedded `turn:`
  server only. We use external `rtc.turn_servers` (coturn), which the new binary
  accepted unchanged. 1.13.6 added TURN config validation; our config passed.
- Prometheus config shape changed in 1.13.8; we do not configure Prometheus.
- 1.13.8 broke egress start (fixed in 1.13.9). Egress is not live here: the HLS
  path in `hls-cast.md` needs a `redis:` section, which was removed on 2026-08-07.
  If egress comes back, re-check the egress/server version matrix first.

## Rollback

Backups: `~/.local/share/livekit-backups/20261025-pre-1.13.11/`
(`livekit-server-1.9.1`, `livekit.yaml`, `SHA256SUMS`), plus
`~/.config/livekit/livekit.yaml.bak-20261025-pre-warp`.

WARP only (keep 1.13.11):

```bash
sed -i '/enable_warp: true/d' ~/.config/livekit/livekit.yaml
systemctl --user restart livekit-server
```

Full rollback to 1.9.1:

```bash
B=~/.local/share/livekit-backups/20261025-pre-1.13.11
(cd $B && sha256sum -c SHA256SUMS)
install -m 755 $B/livekit-server-1.9.1 ~/.local/bin/livekit-server
cp $B/livekit.yaml ~/.config/livekit/livekit.yaml
systemctl --user kill -s SIGKILL livekit-server; systemctl --user restart livekit-server
livekit-server --version   # 1.9.1
```

Then check `lumina-and-chef` has `lumina` back (the agent reconnects by itself)
and that the health probe timer is all green
(`journalctl --user -u skchat-health-probe -n 12`).
