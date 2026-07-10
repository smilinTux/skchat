# skchat cold-machine deploy runbook

Takes a **bare box** (git + skvault access, nothing else) to the full skchat
plane: daemons, both Telegram bridges, Piper TTS, the Nostr discovery relay, the
webui, the LiveKit + coturn call stack, the Flutter web client, and the timers.
It is the operator-facing companion to the reconciled `systemd/` plane, the
`deploy/` secrets tooling, and the scoped-role SQL. Everything here is
reproducible from git: no archaeology of a live `~/.config/systemd/user`.

- Plan reference: `docs/deploy-plan/skchat-bulletproof-deploy.md` (Section 4, P1.3).
- Systemd plane: `systemd/README.md` + `systemd/install.sh`.
- Secrets: `deploy/provision-secrets.sh`, `deploy/render-secrets.py`, `deploy/env-templates/`.
- Scoped PG role: `deploy/sql/skchat_bridge_role.sql` (+ `_rollback.sql`).

> SAFETY: this runbook stands up a **new** host. Do not point the installer or
> the SQL at the live .158 plane as a bulk step. On .158, use
> `systemd/install.sh --diff` and adopt individual units deliberately. The live
> host is at 94% root disk: never run Flutter or other heavy builds on it, use
> .41 (see the Flutter section).

---

## 0. Prerequisites (on the cold box)

| Need | Why | Check |
|------|-----|-------|
| Linux with a user systemd (`loginctl enable-linger $USER`) | user units survive logout | `systemctl --user is-system-running` |
| `git`, `python3.10+`, `python3-venv` | clone + venv | `python3 --version` |
| `~/.skenv` venv (all SK CLIs live here) | `skvault`, `skchat`, `skmemory` | `ls ~/.skenv/bin/skvault` |
| skvault access (KeePass sealed to Chef PGP) + gpg-agent | provisions every secret | `skvault status` |
| Docker (rootless or with socket access) | coturn container is systemd-owned Docker | `docker info` |
| Tailscale up, host joined the tailnet | LiveKit + Nostr bind the tailnet IP | `tailscale ip -4` |
| Postgres reachable (`skmem-pg` on the primary, or a local mirror) | bridge memory path | `psql <dsn> -c 'select 1'` |
| Ollama/mxbai embed endpoint reachable | bridge memory recall embeds queries | see `memory-pg.env` |

If the box is not the Postgres host, it only needs network reach to `skmem-pg`
(`:5432` on the primary, `:5433` on the .41 mirror). It does not run its own PG.

Repo set (clone all four as **siblings** under one parent, the Flutter build
depends on the sibling layout):

```bash
mkdir -p ~/clawd/skcapstone-repos && cd ~/clawd/skcapstone-repos
git clone https://github.com/smilinTux/skchat.git
git clone https://github.com/smilinTux/skchat-app.git      # Flutter client
git clone https://github.com/smilinTux/sk-pqc-dart.git     # PQC Dart pkg (pubspec path dep)
git clone https://github.com/smilinTux/skcomms.git         # transport layer (constraints live here)
```

---

## 1. Python venv + pinned deps (the skcomms constraints gotcha)

skchat runs from the shared `~/.skenv` venv, editable-installed. The one trap:
**skcomms ships `skcomms/constraints.txt` which pins `cryptography==43.0.3`**
(see its header: "pinned dependency set for reproducible installs"). That pin is
correct for skcomms' own reproducible image, but applying it globally can
**downgrade `cryptography`** underneath other tools that share `~/.skenv`
(skmemory, capauth, sequoia bindings). So apply the constraints only when
building an **isolated** deploy venv, never with `-c` against the shared
`~/.skenv`.

### Option A (recommended for a dedicated skchat host): isolated venv

```bash
python3 -m venv ~/.venv-skchat
source ~/.venv-skchat/bin/activate
cd ~/clawd/skcapstone-repos
# Apply the reproducible lock ONLY inside this throwaway venv:
pip install -c skcomms/constraints.txt -e ./skcomms -e ./skmemory -e ./skchat
deactivate
```

Then point the units at this interpreter by exporting `SKENV` / editing the
`ExecStart` python, or symlink `~/.skenv -> ~/.venv-skchat` if this box is
skchat-only.

### Option B (shared `~/.skenv`, the .158 convention): NO global constraints

```bash
cd ~/clawd/skcapstone-repos
~/.skenv/bin/pip install -e ./skcomms -e ./skmemory -e ./skchat   # no -c
# Verify cryptography did NOT get downgraded under a co-tenant:
~/.skenv/bin/python -c "import cryptography, sys; print('cryptography', cryptography.__version__)"
```

If a later resolve ever drags `cryptography` back to 43.0.3 and breaks capauth or
sequoia, that is the constraints leak: reinstall the affected package without the
`-c skcomms/constraints.txt` flag.

Smoke-test the install (run from `~`, never from a dir containing a `skmemory/`
folder, or the namespace collides):

```bash
cd ~ && ~/.skenv/bin/skchat --help >/dev/null && echo "skchat CLI ok"
cd ~ && PYTHONPATH=$PWD/clawd/skcapstone-repos/skchat/src ~/.skenv/bin/python -m pytest \
  clawd/skcapstone-repos/skchat/tests -q -m 'not integration' -x
```

---

## 2. Provision secrets from skvault

No secret value is in git. `deploy/provision-secrets.sh` renders every
EnvironmentFile, the LiveKit config, and the coturn secret from skvault into the
correct paths at `0600`. Templates carry `${skvault:...}` placeholders in
`deploy/env-templates/`.

```bash
cd ~/clawd/skcapstone-repos/skchat
skvault unlock                              # gpg-agent SEAL, once
deploy/provision-secrets.sh unlock-check    # confirms the vault is unlocked
deploy/provision-secrets.sh dry-run         # lists targets + tokens, touches nothing
deploy/provision-secrets.sh check           # resolves every token, writes nothing
deploy/provision-secrets.sh apply           # resolves + writes all files (0600)
# ...or one at a time by short name:
deploy/provision-secrets.sh apply telegram-opus
```

Targets written by `apply` (short name -> path):

| Short name | Path | Holds |
|------------|------|-------|
| `telegram-opus` | `~/.config/skchat/telegram-opus.env` | `TELEGRAM_OPUS_BOT_TOKEN` |
| `telegram-lumina` | `~/.config/skchat/telegram-lumina.env` | `SKC_BRIDGE_TOKEN` |
| `memory-pg` | `~/.config/skchat/memory-pg.env` | `SKMEMORY_PG_DSN` (scoped role, see Section 3) |
| `guest` | `~/.config/skchat/guest.env` | `SKCHAT_GUEST_TOKEN_SECRET` |
| `webui-lumina/opus/chef` | `~/.config/skchat/webui-<agent>.env` | webui config + LiveKit/TURN secrets |
| `livekit` | `~/.config/livekit/livekit.yaml` | LiveKit API key/secret pairs |
| `coturn` | `~/.skchat/coturn/coturn.secret` | coturn shared secret (0600, no trailing newline) |

### Filenames match the drop-ins (no reconciliation needed)

`provision-secrets.sh` writes exactly the paths the committed drop-ins read
(see `systemd/dropins/`):

- `~/.config/skchat/guest.env`  (referenced by `skchat-daemon.d/guest.conf` and `skchat-webui@lumina.d/guest.conf`)
- `~/.config/skchat/memory-pg.env`  (referenced by both `skchat-telegram-*.d/override.conf`)

`systemd/install.sh`'s preflight expects the same two names, so a clean `apply`
lands every file where the units and the installer look for it. No symlink
bridging step.

(The units use the optional `-` EnvironmentFile prefix, so a wrong name would not
crash the service, it would silently degrade: guest links off, memory recall off.
Matching the names is what keeps that from happening.)

The call agent also needs `~/.config/lumina-creative/env` (`NVIDIA_API_KEY`);
provision it from the vault or place it out-of-band before starting
`skchat-lumina-call`.

---

## 3. Create the scoped Postgres role from the committed SQL

The bridges must not use the shared `postgres` superuser DSN. Create the
least-privilege `skchat_bridge` LOGIN role (exactly SELECT/INSERT/UPDATE/DELETE
on `memories`, SELECT on `docs`, nothing else) from the committed, idempotent
SQL. The password is passed as a psql variable so it never lives in the file.

```bash
cd ~/clawd/skcapstone-repos/skchat
# Pick a strong password; store it in skvault as the source of truth for the DSN.
BRIDGE_PW='<STRONG_PASSWORD>'

# Against the skmem-pg container on the PG host:
docker exec -i skmem-pg psql -U postgres -d skmemory \
  -v bridge_pw="$BRIDGE_PW" -f - < deploy/sql/skchat_bridge_role.sql

# ...or over the network to a remote PG host:
psql "postgresql://postgres:<superuser_pw>@<pghost>:5432/skmemory" \
  -v bridge_pw="$BRIDGE_PW" -f deploy/sql/skchat_bridge_role.sql
```

The script prints the resulting grant set (`memories: DELETE,INSERT,SELECT,UPDATE`
and `docs: SELECT`) for verification. Re-running rotates the password idempotently.

Then set `SKMEMORY_PG_DSN` in `memory-pg.env` to the scoped role, and confirm
it authenticates before you start the bridges:

```bash
# memory-pg.env should contain:
#   SKMEMORY_PG_DSN=postgresql://skchat_bridge:<STRONG_PASSWORD>@localhost:5432/skmemory
psql "postgresql://skchat_bridge:${BRIDGE_PW}@localhost:5432/skmemory" -c '\dp memories' \
  && echo "scoped role authenticates"
```

(`provision-secrets.sh` can build this DSN for you via
`SKCHAT_BRIDGE_DSN_TEMPLATE`, default
`postgresql://skchat_bridge:{pw}@localhost:5432/skmemory`, if the password is in
the vault.)

---

## 4. Install the systemd units via the reconcile installer

`systemd/install.sh` materializes every unit, drop-in, timer, the coturn start
script, and the webui register hook into `~/.config/systemd/user`. It is
idempotent, copies only on content change, verifies each unit with
`systemd-analyze --user verify`, runs a secret preflight, and **never restarts a
running unit**. Units are `%h`/`%i`-relative: no absolute paths, no username
assumptions.

```bash
cd ~/clawd/skcapstone-repos/skchat/systemd
./install.sh --dry-run      # print planned actions, write nothing
./install.sh --diff         # show drift vs any already-installed copies
./install.sh                # install + daemon-reload (no enable, no start)
```

The secret preflight will WARN (not fail) for any missing EnvironmentFile. On a
correctly provisioned box every line should read `[OK]`. If you see
`[MISS] .config/skchat/guest.env` or `.config/skchat/memory-pg.env`, you skipped
`provision-secrets.sh apply` in Section 2 (or ran it against a locked vault).

To enable + start the live-enabled set (daemons, both bridges, call, webui@lumina,
piper, nostr, app-web, livekit, coturn, jarvis-heartbeat, telegram-catchup.timer):

```bash
./install.sh --enable          # enable only
./install.sh --enable --start  # enable + start units that are not already running
```

`skchat-daemon-chef` and `telegram-catchup.service` (static) are shipped but left
disabled, matching .158.

### coturn adoption note

coturn is a **systemd-owned Docker container** (oneshot + `RemainAfterExit`,
`start-coturn.sh` launches with `--restart no`). On a cold box there is no
pre-existing container, so `--enable --start` just works. If a container named
`skchat-coturn` already exists from a manual run, remove it once so systemd is
the sole supervisor: `docker rm -f skchat-coturn` then
`systemctl --user restart skchat-coturn.service`.

---

## 5. Start services in dependency order

`--enable --start` starts everything, but if you are bringing the plane up
by hand (or debugging), respect this order. Later tiers assume earlier ones are
healthy.

```bash
U() { systemctl --user "$@"; }

# Tier 0: transport + discovery (no deps beyond the venv + network)
U start skchat-nostr-relay.service        # :7447 discovery relay (binds tailnet IP)
U start skchat-piper-tts.service          # :18797 CPU TTS

# Tier 1: the receive daemons (skcomms transport comes up inside them)
U start skchat-daemon.service             # :9385 lumina  (+ :9384 skcomms health)
U start skchat-daemon-opus.service        # :9388 opus

# Tier 2: bridges (need daemon + memory-pg DSN + bot tokens)
U start skchat-telegram-opus.service
U start skchat-telegram-lumina.service

# Tier 3: call stack (livekit before the call agent; coturn before guest calls)
U start livekit-server.service            # tailnet :7880 / :7881 (wait-tailnet gate)
U start skchat-coturn.service             # TURN relay (Docker)
U start skchat-lumina-call.service        # LiveKit voice agent (needs NVIDIA_API_KEY)

# Tier 4: user-facing surfaces
U start skchat-webui@lumina.service       # web UI + voice chat
U start skchat-app-web.service            # :8088 Flutter web static (needs the build, Section 7)

# Agents + timers
U start jarvis-heartbeat.service
U start telegram-catchup.timer
```

`livekit-server` has an `ExecStartPre` tailnet health gate (`wait-tailnet.conf`),
so if Tailscale is not up it will wait rather than bind the wrong IP.

---

## 6. Bootstrap verification checklist

Run top to bottom. Every line should be green before you call the box deployed.

### Unit state

```bash
systemctl --user --no-pager --state=failed list-units   # must be EMPTY
systemctl --user is-active \
  skchat-daemon.service skchat-daemon-opus.service \
  skchat-telegram-opus.service skchat-telegram-lumina.service \
  skchat-piper-tts.service skchat-nostr-relay.service \
  skchat-app-web.service livekit-server.service skchat-coturn.service \
  skchat-webui@lumina.service jarvis-heartbeat.service
systemctl --user is-enabled telegram-catchup.timer
```

### Health endpoints

| Check | Command | Expect |
|-------|---------|--------|
| skchat daemon (lumina) | `curl -fsS http://localhost:9385/health` | 200, transport ok |
| skcomms transport | `curl -fsS http://localhost:9384/health` | 200, green transport |
| Flutter web origin | `curl -fsS -o /dev/null -w '%{http_code}\n' http://localhost:8088/` | `200` |
| Piper TTS | `curl -fsS http://localhost:18797/v1/audio/voices \| head -c 200` | JSON voices list |
| Nostr relay | see WebSocket check below | upgrades to ws |
| opus daemon | `curl -fsS http://localhost:9388/health` | 200 |

Nostr binds the **tailnet IP**, not loopback (`override.conf`), so hit it there:

```bash
TSIP=$(tailscale ip -4 | head -1)
curl -fsS -i --http1.1 \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" -H "Sec-WebSocket-Version: 13" \
  "http://${TSIP}:7447/" | head -1        # expect: HTTP/1.1 101 Switching Protocols
ss -ltnp | grep -E ':7447' && echo "nostr listening on ${TSIP}:7447"
```

### LiveKit on the tailnet

```bash
TSIP=$(tailscale ip -4 | head -1)
ss -ltnp | grep -E ':7880|:7881'                       # SFU + RTC listening
curl -fsS -o /dev/null -w '%{http_code}\n' "http://${TSIP}:7880/"   # LiveKit responds
grep -c '^' ~/.config/livekit/livekit.yaml && echo "livekit config present"
journalctl --user -u livekit-server -n 20 --no-pager | grep -i "starting\|listening"
```

### coturn (TURN relay)

```bash
docker ps --filter name=skchat-coturn --format '{{.Names}} {{.Status}}'   # Up
systemctl --user show skchat-coturn.service -p ActiveState -p SubState    # active / running (RemainAfterExit)
ss -lun | grep -E ':3478' && echo "coturn UDP 3478 listening"
test -r ~/.skchat/coturn/coturn.secret && echo "coturn secret present (0600)"
stat -c '%a' ~/.skchat/coturn/coturn.secret                               # expect 600
```

### Bridge liveness (the wedge-critical path)

```bash
journalctl --user -u skchat-telegram-opus -n 40 --no-pager | grep -i "brain ready"
# "brain ready" plus "N tools exposed" == the consciousness bridge came up with its MCP tools.
journalctl --user -u skchat-telegram-lumina -n 40 --no-pager | grep -i "brain ready"
```

Then send each bot one Telegram message and confirm a reply within ~30 s (the
`asyncio.wait_for(45)` seatbelt bounds a hung poll). Confirm memory recall by
checking the bridge logs mention a `memory_search` hit, which proves the scoped
`skchat_bridge` DSN authenticated.

### End-to-end call smoke (optional, needs two peers)

Follow `runbooks/browser-call-test.md`: open the webui `/pair` page, pair two
identities, place a call. The ICE ladder (`src/skchat/connectivity.py`) should
land on Tailscale/LAN, falling back to sovereign coturn ephemeral creds. If it
falls through to `openrelay.metered.ca`, your coturn UDP path is not reachable
(known gap G8, plan P3.1).

---

## 7. Flutter web client build (BUILD ON .41, NOT the primary)

`.158` is at 94% root disk: a Flutter build can wedge it (gap G18). Build the web
artifact on a box with headroom (.41, `ssh laptop`) and copy the result, or build
locally only if this cold box has disk to spare.

### Sibling-repo layout (required)

`skchat-app/pubspec.yaml` declares `sk_pqc` as a git dependency and a
`dependency_overrides` **path** dep pointing at `../sk-pqc-dart` (the real
package name is `sk_pqc`; the GitHub repo was renamed to `sk-pqc-dart`). So the
two repos MUST be siblings:

```
~/clawd/skcapstone-repos/
  skchat-app/        <- Flutter client
  sk-pqc-dart/       <- pubspec resolves ../sk-pqc-dart  (do NOT rely on the stray ../sk_pqc symlink)
```

A `sk_pqc -> sk-pqc-dart` symlink may exist; the override points at the real dir
`../sk-pqc-dart` so it resolves identically on any machine that cloned the sibling.

### Build the PQC web shim (`sk_pqc_noble.js`)

The web PQC backend needs `web/sk_pqc_noble.js`, an esbuild bundle of
`@noble/post-quantum`'s ml_kem768 exposed as `globalThis.skPqc`. It is built from
`skchat-app/web/pqc/`:

```bash
cd ~/clawd/skcapstone-repos/skchat-app/web/pqc
npm ci                 # installs @noble/post-quantum 0.6.0 + esbuild 0.25.0 (pinned)
npm run build          # esbuild bootstrap.js --bundle --format=esm --minify --outfile=../sk_pqc_noble.js
```

`web/index.html` loads it via `<script type="module" src="sk_pqc_noble.js">`.

### Build the Flutter web bundle

```bash
cd ~/clawd/skcapstone-repos/skchat-app
flutter pub get
flutter build web --release        # emits build/web/ (includes sk_pqc_noble.js from web/)
```

`skchat-app-web.service` serves `%h/clawd/skcapstone-repos/skchat-app/build/web`
on `:8088`. If you built on .41, rsync the `build/web/` tree to the serving box
at that exact path, then `systemctl --user restart skchat-app-web.service`.

> Web PQC is a documented reduced-assurance leg (WebCrypto has no PQC; native
> gets full hybrid via liboqs FFI). See `docs/crypto-architecture.md`.

---

## 8. Rollback

Rollback is per-tier and non-destructive. The installer never restarts running
units, so "undo" is explicit.

### Stop / disable the plane

```bash
U() { systemctl --user "$@"; }
# Stop user-facing first, transport last (reverse of Section 5):
U stop skchat-app-web.service skchat-webui@lumina.service \
       skchat-lumina-call.service skchat-coturn.service livekit-server.service \
       skchat-telegram-opus.service skchat-telegram-lumina.service \
       skchat-daemon-opus.service skchat-daemon.service \
       skchat-piper-tts.service skchat-nostr-relay.service jarvis-heartbeat.service
U stop telegram-catchup.timer

# Disable if you are decommissioning (keeps the unit files):
./install.sh --diff   # confirm what is installed first
for u in skchat-daemon skchat-daemon-opus skchat-telegram-opus skchat-telegram-lumina \
         skchat-lumina-call skchat-nostr-relay skchat-piper-tts livekit-server \
         skchat-coturn jarvis-heartbeat skchat-app-web; do
  U disable "$u.service"
done
U disable skchat-webui@lumina.service telegram-catchup.timer
U daemon-reload
```

### Remove the unit files

```bash
# The installer only writes; removal is manual and deliberate.
rm -f ~/.config/systemd/user/skchat-*.service \
      ~/.config/systemd/user/livekit-server.service \
      ~/.config/systemd/user/jarvis-heartbeat.service \
      ~/.config/systemd/user/telegram-catchup.{service,timer}
rm -rf ~/.config/systemd/user/skchat-*.service.d \
       ~/.config/systemd/user/livekit-server.service.d
systemctl --user daemon-reload
```

### Roll back the scoped PG role

```bash
# FIRST cut the bridges back to a working DSN (shared postgres, or leave them stopped),
# otherwise the memory path fails to authenticate once the role is gone.
docker exec -i skmem-pg psql -U postgres -d skmemory \
  -f - < ~/clawd/skcapstone-repos/skchat/deploy/sql/skchat_bridge_role_rollback.sql
# Idempotent: no-ops with a NOTICE if the role was never created.
```

### Roll back coturn to Docker-native supervision

```bash
U stop skchat-coturn.service && U disable skchat-coturn.service
docker start skchat-coturn 2>/dev/null || \
  ~/.skchat/coturn/start-coturn.sh   # (edit --restart no back to unless-stopped if you want Docker to own it)
```

### Secrets

Secrets are regenerable, not rolled back: re-run `provision-secrets.sh apply`
to restore from the vault, or `rm ~/.config/skchat/*.env` to wipe them (the
optional-`-` EnvironmentFile prefix means the services degrade rather than crash
without them). Never commit any of these files.

---

## Appendix: port map

| Port | Bind | Service |
|------|------|---------|
| 9385 | localhost | skchat-daemon (lumina) health |
| 9388 | localhost | skchat-daemon-opus health |
| 9389 | localhost | skchat-daemon-chef (disabled) |
| 9384 | localhost | skcomms transport health |
| 18797 | localhost | Piper CPU TTS (`/v1/audio/speech`) |
| 7447 | tailnet IP | Nostr discovery relay (ws) |
| 8088 | 0.0.0.0 | Flutter web static (dev server, P3.3 replaces) |
| 7880 / 7881 | tailnet IP | LiveKit SFU + RTC |
| 3478 (udp) | host | coturn TURN relay |
| 15090 | localhost | legacy `piper-tts.service` (DEPRECATED, not installed) |
