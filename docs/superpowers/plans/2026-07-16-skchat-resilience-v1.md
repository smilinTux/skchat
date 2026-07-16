# skchat Resilience v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the shipped, live skchat comms plane (calling, all 3 invite modes, message-log, HLS, public 443) production-durable and reproducible: nothing irreplaceable is un-backed-up, live services self-recover and are externally probed, and the sovereign ingress is codified.

**Architecture:** No product changes. This hardens the operational plane: a backup/restore pair for the irreplaceable stateful data, watchdog + escalation on the wedge-prone bridges, an external health probe with alerting, a sovereign-only TURN path, and the tailnet ingress + node bootstrap captured in the repo.

**Tech Stack:** bash + systemd (user units + timers), PGP (sq/gpg) for encrypting the backup, `sk-alert` for notifications, existing `scripts/check-health.sh` / `qa_suite.sh`, coturn (Funnel), the skchat Python daemon/bridge.

## Global Constraints

- NO em or en dashes in any file, comment, commit, or doc. Plain hyphens only.
- Commit trailer: `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`.
- Run on .158 (noroc2027), the live plane. Services are `systemctl --user`. Roll changes with `systemctl --user daemon-reload` + restart of the touched unit only.
- Units live in `systemd/units/`, drop-ins in `systemd/dropins/`, reconciled by `systemd/install.sh` (`--dry-run`/`--diff`/`--enable`). Every new unit/timer ships through the installer, secrets externalized.
- The backup encrypts to Chef's PGP recipient; never write group keys or at-rest keys to an unencrypted archive.
- Each task maps to a coord board task id (in the heading); mark it done via `skcapstone coord` when the task ships.

## Priority order (why this sequence)

Backup first (a disk loss right now is unrecoverable), then live-plane self-recovery + external eyes, then sovereignty + reproducibility. Medium/low items (coturn unit reconcile, boot-ordering, runtime-artifact provisioning, nostr persistence, HA warm-standby, nightly QA, CI for scripts, disk guard, cold-start docs) are Resilience v2, listed at the end.

---

## Task 1: Backup and restore of live stateful data (coord a65aa49f, CRITICAL)

**Files:**
- Create: `scripts/backup-skchat.sh`
- Create: `scripts/restore-skchat.sh`
- Create: `systemd/units/skchat-backup.service`, `systemd/units/skchat-backup.timer`
- Modify: `systemd/install.sh` (register the new unit + timer)
- Test: `scripts/test-backup-roundtrip.sh`

**What it protects (from the live `~/.skchat`):** `atrest_dek.wrap`, `atrest_recipient.key`/`.pub` (at-rest DEK + keys), `consumed_nonces.db`, `confs.json`, group-key stores + message-log/history DB, `coturn/`, plus `~/.skcomms/outbox` and `~/.config/skchat/*.env` (bot tokens). Losing the at-rest keys means encrypted history is unrecoverable, hence CRITICAL.

- [ ] **Step 1: Write `backup-skchat.sh`** — tar the target set into a temp archive, encrypt to Chef's PGP recipient (`sq encrypt --recipient-file` or `gpg -e -r chef@skworld.io`), write `~/.skchat-backups/skchat-<STAMP>.tar.gz.pgp` (STAMP passed in via `--stamp` since the daemon environment forbids `date` in some contexts; the timer passes `%i`/now). Prune backups older than 14 days. Never leave the unencrypted tar on disk (`trap` cleanup).

- [ ] **Step 2: Write `restore-skchat.sh`** — takes an encrypted archive path, decrypts (prompts for the PGP passphrase / uses gpg-agent), extracts to a `--target` dir (default a scratch dir, NOT the live `~/.skchat`, to force an explicit confirm before overwriting live).

- [ ] **Step 3: Round-trip test `test-backup-roundtrip.sh`** — seed a scratch home with a marker file, run backup against it, wipe, restore into another scratch dir, assert the marker file content matches. Run: `bash scripts/test-backup-roundtrip.sh` ; Expected: `ROUNDTRIP OK`.

- [ ] **Step 4: Units** — `skchat-backup.service` (Type=oneshot, runs `backup-skchat.sh`), `skchat-backup.timer` (daily 04:30, `Persistent=true`), `notify: on_failure` via an ExecStopPost sk-alert on failure. Register both in `install.sh`.

- [ ] **Step 5: Enable + verify one real backup** — `systemd/install.sh --enable`; `systemctl --user start skchat-backup.service`; confirm one `*.tar.gz.pgp` appears and decrypts. Commit.

---

## Task 2: Bridge wedge escalation (coord 8ee5151c, HIGH)

**Files:**
- Modify: `scripts/telegram_bridge.py` (sd_notify heartbeat + poll-failure counter + exit-to-restart)
- Modify: `systemd/units/skchat-telegram@.service` (+ the -opus/-lumina units): `WatchdogSec`, `Restart=on-failure`, `NotifyAccess=main`, `Type=notify`
- Create: `systemd/dropins/telegram-watchdog.conf` (shared drop-in)

The bridges wedge silently when the poll hangs on `ConnectTimeout` (known incident). Make systemd kill+restart a wedged bridge and alert.

- [ ] **Step 1:** In `telegram_bridge.py`, on startup call `sd_notify("READY=1")`; after each successful poll cycle call `sd_notify("WATCHDOG=1")`. Wrap the poll in `asyncio.wait_for`; on N consecutive failures (default 3) log + `sk-alert` "bridge <agent> wedged, exiting for restart" and `sys.exit(1)`.

- [ ] **Step 2:** Unit drop-in: `Type=notify`, `WatchdogSec=90`, `Restart=on-failure`, `RestartSec=5`, `StartLimitIntervalSec=300`, `StartLimitBurst=5`. If the process stops heartbeating for 90s, systemd restarts it.

- [ ] **Step 3: Verify** — `systemctl --user restart skchat-telegram-opus`; `journalctl --user -u skchat-telegram-opus -f` shows `WATCHDOG=1` heartbeats; simulate a wedge (block the poll) and confirm systemd restarts within ~90s + an sk-alert fires. Commit.

---

## Task 3: Scheduled external health probe (coord 24f3159f, HIGH)

**Files:**
- Create: `scripts/health-probe.sh` (extends `check-health.sh` into a pass/fail probe over ALL live endpoints)
- Create: `systemd/units/skchat-health-probe.service` + `.timer`
- Modify: `systemd/install.sh`

- [ ] **Step 1:** `health-probe.sh` curls each live surface and exits non-zero on any failure: daemon `:9385/health`, skcomms `:9384/health`, webui (per-agent), coturn (turnutils or a TCP check on the Funnel `:8443`), livekit `:7880`, nostr relay `:7447`, piper tts `:18797`, and the public HTTPS ingress. Print a GREEN/RED table.

- [ ] **Step 2:** Timer every 5 min; `notify: on_failure` -> `sk-alert` with the failing surface. Register in `install.sh`.

- [ ] **Step 3: Verify** — `bash scripts/health-probe.sh` prints all-green live; stop one service and confirm it goes RED + alerts. Commit.

---

## Task 4: Codify tailscale serve/funnel ingress (coord 56c6d755, HIGH)

**Files:**
- Create: `systemd/tailscale-ingress.sh` (idempotent `tailscale serve`/`funnel` apply)
- Create: `systemd/TAILSCALE-INGRESS.md` (the ingress map + rationale)

The current Funnel (guest links `/`, `/daemon`, `/livekit-ws`, `/.well-known/skfed/directory`, TCP `:8443`/`:10000`) exists only in the live `tailscale serve` state, not the repo. Capture it so the ingress is reproducible on a rebuild.

- [ ] **Step 1:** Snapshot the current config: `tailscale serve status` + `tailscale funnel status`; translate each mapping into idempotent `tailscale serve --bg ...` / `tailscale funnel` commands in `tailscale-ingress.sh` (guarded so re-running is a no-op).

- [ ] **Step 2:** Document each mapping (path -> backend, why public vs tailnet-only) in `TAILSCALE-INGRESS.md`, including the coturn `:8443` and livekit wss legs.

- [ ] **Step 3: Verify** — `bash systemd/tailscale-ingress.sh` on .158 reproduces the current `tailscale serve status` with no diff. Commit.

---

## Task 5: Sovereign public TURN path, retire openrelay (coord 10386e96, HIGH)

**Files:**
- Modify: `src/skchat/connectivity.py` (`ice_config()` ICE ladder: drop the openrelay fallback, keep sovereign coturn Funnel ephemeral creds)
- Modify: `runbooks/browser-call-test.md` (note the TURN path is sovereign-only)
- Test: `tests/test_connectivity.py`

- [ ] **Step 1:** In `ice_config()`, remove the `openrelay.metered.ca` (or equivalent) STUN/TURN entries; keep the tier ladder Tailscale -> LAN -> sovereign coturn (ephemeral HMAC creds via the Funnel `:8443`). Fail closed to sovereign coturn.

- [ ] **Step 2: Test** — assert `ice_config()` returns no non-sovereign TURN hosts and includes the coturn Funnel URL. Run: `pytest tests/test_connectivity.py -q`.

- [ ] **Step 3: Verify a real call** — run the browser-call test (per `runbooks/browser-call-test.md`) over the sovereign TURN only; confirm media connects. Commit.

---

## Task 6: Node identity + agent-home bootstrap chain (coord bc3c7454, HIGH)

**Files:**
- Create: `scripts/bootstrap-node.sh`
- Create: `docs/BOOTSTRAP.md`

Resolve the skvault chicken-and-egg (identity needs skvault, skvault needs identity) so a blank machine can join the plane deterministically.

- [ ] **Step 1:** Document the dependency order in `BOOTSTRAP.md`: (1) install `~/.skenv` packages, (2) provision CapAuth identity / import the agent PGP key, (3) unlock skvault with that key, (4) restore `~/.skchat` from Task-1 backup or provision fresh, (5) `systemd/install.sh --enable`, (6) `tailscale-ingress.sh`. Call out exactly where the chicken-and-egg is broken (the key import before skvault unlock).

- [ ] **Step 2:** `bootstrap-node.sh` runs the ordered steps with preflight checks (fails early with a clear message if a prerequisite is missing). Idempotent.

- [ ] **Step 3: Verify** — dry-run on a scratch `SKCAPSTONE_HOME` (or a throwaway user) to confirm the ordering and preflight messages are correct. Commit.

---

## Task 7: Proper web-serving unit for the Flutter client (coord b5078963, MEDIUM)

**Files:**
- Modify: `systemd/units/skchat-app-web.service` (replace the dev `python -m http.server`)
- Create: `scripts/serve-app-web.sh` (a hardened static server: correct MIME + `--base-href /app/`, `Cache-Control` for hashed assets, no directory listing, bound to loopback/tailnet)

- [ ] **Step 1:** Replace the stopgap dev server with `serve-app-web.sh` (a small hardened static server, still stdlib or a vendored one-file server) serving the versioned `skchat-app/build/web` with correct headers and no autoindex.

- [ ] **Step 2:** Point the unit at the script; keep `:8088`; add `Restart=on-failure`. Register via `install.sh`.

- [ ] **Step 3: Verify** — `curl -I` the served client shows correct `Content-Type` + `Cache-Control`; the app loads. Commit.

---

## Deferred: Resilience v2 (follow-on plan)

Medium/low deploy-hardening items, sequence after v1: reconcile coturn supervision + commit the unit (96fac90f), declare upstream dependencies + boot ordering (f029c873), provision third-party runtime artifacts + wire install-livekit.sh (af557d9b), persist nostr relay state across restarts (b0030a6d), warm standby on .41 with failover runbook (4dad1fd6), nightly live QA on a timer with alerting (562f198a, builds on `qa_suite.sh`), lint+test `scripts/` in CI (7aa0ae53), disk headroom guard on .158 (3f0b2c81), Flutter cold-start docs (2a1b7227).

Separate epics (not deploy-hardening, sequence independently): the skchat-unified consolidation (21 items, batches A-G), hermes-skchat P4/P5 (7), PQC migration onto published packages (3e5f1f16), SK Spaces S4, the SKStacks-2027 design-system adoption.

## Self-Review

- **Coverage:** All 6 CRITICAL/HIGH deploy-hardening items are Tasks 1-6; one MEDIUM (web-serving unit) is Task 7 because it is small and closes a known stopgap. Remaining medium/low are the explicit v2 list.
- **No placeholders:** each task names exact files, the concrete action, and a verification command.
- **Sequencing:** backup precedes everything (data-loss risk); self-recovery + external probe before sovereignty/reproducibility; each task is independently shippable and coord-tracked.
