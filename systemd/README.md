# skchat systemd plane (reconciled from live .158)

This directory is the source of truth for every systemd user unit that runs the
skchat plane on the primary host (.158, `noroc2027`). It was reconciled from the
live `~/.config/systemd/user` topology on 2026-07-10 (plan task P1.1 / coord
`3d5667cc`). A cold machine with git access and provisioned secrets can stand up
the whole plane with `./install.sh`.

## Layout

```
systemd/
  install.sh                 idempotent installer (--dry-run, --diff, --enable, --start)
  units/                     one file per live unit (+ go-forward @ templates)
  dropins/<unit>.d/*.conf    live drop-ins, secrets externalized
  coturn/start-coturn.sh     coturn container launcher (systemd-owned)
  coturn/coturn.secret.example
  examples/livekit.yaml.example
  examples/register_webui.py webui ExecStartPost service-registry hook
  examples/env/*.env.example names-only EnvironmentFile templates
```

Everything is `%h`/`%i`-relative: no `/home/<user>` and no username assumptions.
systemd resolves the specifiers at runtime, so the installed files are portable
across hosts and users.

## What runs (the reconciled plane)

| Unit | Port / role | Enabled on .158 | Notes |
|------|-------------|-----------------|-------|
| `skchat-daemon.service` | :9385, lumina receive daemon | yes | store `~/.skchat` |
| `skchat-daemon-opus.service` | :9388, opus receive daemon | yes | isolated `~/.skchat-opus` |
| `skchat-daemon-chef.service` | :9389, chef receive-only | no (disabled) | isolated `~/.skchat-chef`; shipped, not enabled |
| `skchat-telegram-opus.service` | Opus Telegram bridge | yes | @seaBird_Opus_bot, qwen3.6 @ .100:8082 |
| `skchat-telegram-lumina.service` | Lumina Telegram bridge | yes | @seaBird_Lumi_bot, default role sk-creative |
| `skchat-lumina-call.service` | LiveKit voice agent | yes | lumina-creative `lumina-call.py` |
| `skchat-webui@lumina.service` | web UI + voice chat | yes | template `skchat-webui@.service` |
| `skchat-piper-tts.service` | :18797 Piper CPU TTS | yes | canonical TTS |
| `skchat-nostr-relay.service` | :7447 discovery relay | yes | in-memory; binds host tailnet IP |
| `skchat-app-web.service` | :8088 Flutter web static | yes | hardened stdlib server (`scripts/serve-app-web.sh`), loopback bind, no autoindex |
| `livekit-server.service` | tailnet :7880/:7881 SFU | yes | config holds 3 API keys |
| `skchat-coturn.service` | TURN relay (Docker) | yes | systemd-owned container (see below) |
| `jarvis-heartbeat.service` | inbox poll -> tmux Claude | yes | MemoryMax 512M |
| `telegram-catchup.timer` | daily 06:00 import | yes | fires `telegram-catchup.service` |
| `telegram-catchup.service` | oneshot import | static | cross-repo dep on skcapstone |

### Drop-ins carried over

- `skchat-daemon.service.d/`: `guest.conf` (guest links; secret externalized),
  `group.conf` (group chat backend), `dm-ratchet.conf` (RFC-0001 P1 DM ratchet).
- `skchat-daemon-opus.service.d/group.conf`.
- `skchat-telegram-opus.service.d/override.conf`,
  `skchat-telegram-lumina.service.d/override.conf`: rating buttons + skmem-pg
  memory backend; the inline `SKMEMORY_PG_DSN` secret is externalized.
- `skchat-lumina-call.service.d/`: `fixes.conf`, `musetalk.conf`, `tts.conf`,
  `vad.conf`, `webui.conf` (later-loaded drop-ins win: effective TTS is
  localhost:15091, webui 127.0.0.1:8765, MuseTalk disabled).
- `skchat-nostr-relay.service.d/override.conf`: rebind to the host tailnet IP.
- `skchat-webui@lumina.service.d/guest.conf`: guest links (same shared secret as
  the daemon).
- `livekit-server.service.d/wait-tailnet.conf`: gate startup on the tailnet IP.

## Secrets

No secret value is committed. Every secret is a `${VAR}` in a `-EnvironmentFile`
(optional prefix, so a missing file degrades rather than downs the service).
Provision these on the host before enabling (templates in `examples/env/`):

| File | Holds |
|------|-------|
| `~/.config/skchat/guest.env` | `SKCHAT_GUEST_TOKEN_SECRET` (shared: daemon + webui) |
| `~/.config/skchat/memory-pg.env` | `SKMEMORY_PG_DSN` (both telegram bridges) |
| `~/.config/skchat/telegram-opus.env` | `TELEGRAM_OPUS_BOT_TOKEN` |
| `~/.config/skchat/telegram-lumina.env` | `SKC_BRIDGE_TOKEN` |
| `~/.config/skchat/webui-<agent>.env` | webui config + LiveKit/TURN secrets (`SKCHAT_PORT` required) |
| `~/.config/livekit/livekit.yaml` | LiveKit API key/secret pairs |
| `~/.config/lumina-creative/env` | `NVIDIA_API_KEY` (call agent) |
| `~/.skchat/coturn/coturn.secret` | coturn shared secret (0600) |

The two live inline secrets that this reconcile externalized were the two
`SKCHAT_GUEST_TOKEN_SECRET` drop-ins and the two `SKMEMORY_PG_DSN` bridge
drop-ins. Rotating the previously-inline PG DSN is plan task P1.2.

`install.sh` runs a preflight and warns (does not fail) for any missing file.

## coturn ownership decision (G10 split-brain fix)

On .158 the coturn Docker container was started out-of-band with
`--restart unless-stopped` while `skchat-coturn.service` sat inactive: two
supervisors, neither authoritative. This repo makes **systemd the single owner**:

- `skchat-coturn.service` is a `oneshot` + `RemainAfterExit=yes` unit whose
  `ExecStart` runs `coturn/start-coturn.sh`; `ExecStop` runs `docker stop`.
- `start-coturn.sh` launches the container with `--restart no`, so Docker never
  auto-restarts it. `Restart=on-failure` on the unit is the only supervisor.

To adopt on .158: `docker rm -f skchat-coturn` once, then
`systemctl --user enable --now skchat-coturn.service`. Same realm, same secret,
same ports; only the supervision owner changes.

## Deprecated: `piper-tts.service` (:15090)

The legacy `piper-tts.service` (uvicorn from `~/piper-server`, port 15090) is a
duplicate of `skchat-piper-tts.service` (:18797) and is **not shipped or
installed** here. Only the :18797 unit is canonical. If the legacy unit still
exists on a host, retire it: `systemctl --user disable --now piper-tts.service`.

## Concrete units vs templates

The live plane uses **concrete** per-agent unit names (`skchat-daemon-opus`,
`skchat-telegram-opus`, `skchat-telegram-lumina`) rather than instanced
templates, because they carry per-agent LLM/memory config and isolated home
dirs. We ship them faithfully so installing over the live plane is a zero
functional change. A go-forward, fully-portable `skchat-telegram@.service`
template is also shipped (and installed, but not enabled) for a future migration
to `skchat-telegram@<agent>` once the per-agent config moves entirely into the
EnvironmentFiles. A `skchat-daemon@.service` unification is possible the same way
but is deferred to preserve the running units' identities.

## Usage

```
./install.sh --diff        # show drift between repo and installed copies
./install.sh --dry-run     # print planned actions, write nothing
./install.sh               # install + daemon-reload (no enable, no start)
./install.sh --enable      # + enable the live-set units
./install.sh --enable --start   # + start units that are not already running
```

`install.sh` never restarts a running unit: it copies (only on content change),
`daemon-reload`s, and optionally enables/starts inactive units. Restarting to
pick up a changed unit is a deliberate, separate operator step.

SAFETY: do not run the real installer against the live .158 plane inside an
automated task. Validate with `--dry-run` / `--diff` first; do real install
tests on a spare host.
