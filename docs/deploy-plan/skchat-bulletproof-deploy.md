# skchat Bulletproof Deployment Plan

Date: 2026-07-10
Repo: `~/clawd/skcapstone-repos/skchat` (plus its Flutter client `~/clawd/skcapstone-repos/skchat-app`)
Scope: daemon, Telegram bridges, piper TTS, nostr relay, webui, conf/call stack (LiveKit + coturn), Flutter web client, CI, secrets.

## 1. Current State

Honest one-paragraph version: skchat works well and is deployed nowhere near bulletproof. All 7 user services are green on .158 (skchat-daemon, both telegram bridges, piper-tts :18797, nostr-relay :7447, webui@lumina, jarvis-heartbeat) plus the call stack (livekit-server on the tailnet IP, coturn in Docker, app-web on :8088). The known Telegram silent-wedge is genuinely mitigated (asyncio.wait_for(45) around `_poll` at `scripts/telegram_bridge.py:1038` plus fresh httpx clients per poll in the skcomms adapter). The Python test suite is large and healthy (2,420 tests, clean marker discipline, 4 workflows). Secrets scanned clean in git; bot tokens live in EnvironmentFiles.

But the deployment is hand-crafted and unreproducible:

- The live service topology exists only in `~/.config/systemd/user` on .158. The repo `systemd/` dir ships 4 stale units: the bridge units point at a dead path (`%h/dkloud.douno.it/p/smilintux-org/skchat/...`, verified) and the daemon unit sets `Type=forking` + `WatchdogSec=30` while `grep -rn "sd_notify\|WATCHDOG" src/ scripts/` returns nothing (verified), so a fresh install watchdog-kills the daemon in a loop.
- `scripts/telegram_bridge.py:31-32` hardcodes `sys.path.insert(0, "/home/cbrd21/clawd/skcapstone-repos/...")` (verified), so the bridge cannot start on another machine or username.
- `.github/workflows/publish.yml:23` runs `pytest ... || true` (verified): a fully red suite still publishes to PyPI/npm.
- The Flutter client (`skchat-app`) has no `.github` directory at all: 60 test files and `flutter analyze` never run in CI, and prod web is a `python3 -m http.server 8088` serving the dev checkout's `build/web`.
- Public conf-call media falls back to the free `openrelay.metered.ca` TURN (verified in `src/skchat/connectivity.py:69-71`) because sovereign coturn UDP is closed to the internet.
- The nostr relay is explicitly in-memory (verified, `scripts/nostr_relay.py` docstring: events lost on restart).
- coturn supervision is split-brain: systemd unit inactive(dead) while the Docker container runs on `--restart unless-stopped`.
- Everything is a single instance on .158, which is also at 94% root disk. Loss of .158 takes down Telegram presence, TTS, discovery, webui, SFU, TURN, and the web origin at once.

## 2. Target: what bulletproof means for skchat

1. **Reproducible from scratch.** A cold machine with git access and skvault can stand up every live service (daemon, both telegram bridges, piper, nostr, webui@, livekit, coturn, app-web, timers) from the repo: templated units, an idempotent install script, no path or username assumptions, no archaeology of `~/.config/systemd/user`.
2. **Secrets never in git.** Every secret (bot tokens, LiveKit API secret, guest operator token, Postgres DSN) is provisioned from skvault/OpenBao into EnvironmentFiles by a documented script; the repo carries names-only templates. The inline PG DSN currently in a live drop-in gets rotated and moved.
3. **No single point of failure.** Warm standby for the bridge/TTS/relay plane on .41 with a documented (initially manual) failover, single-poller discipline for Telegram, and durable relay state so a restart is free.
4. **CI-gated.** Red tests block release (kill the `|| true`), `scripts/` is linted and its wedge-critical paths tested, and the Flutter client gets analyze + test + build-web CI so a clean clone provably builds.
5. **Observable and self-recovering.** The wedged-but-active failure class is detected (sd_notify watchdog pings from the poll loop, consecutive-failure counter that exits for systemd to restart), a scheduled external health probe covers every live port, and failures page via sk-alert instead of waiting for Chef to notice missed messages.
6. **Documented for a cold machine.** One runbook takes a bare box to full stack, including the Flutter client's sibling-repo `sk_pqc` layout and `sk_pqc_noble.js` build path.

## 3. Gap Analysis (severity-ordered)

| # | Severity | Area | Gap |
|---|----------|------|-----|
| G1 | critical | Deploy reproducibility | Live topology (9+ units, drop-ins, timers) exists only on .158; repo ships 4 stale units with dead ExecStart paths. Cold-machine deploy is impossible from git. |
| G2 | critical | Flutter CI | skchat-app has zero CI: 60 test files, analyze, and `flutter build web` never gated. Regressions ship silently. |
| G3 | high | Release gating | publish.yml `pytest ... || true` makes the PyPI/npm gate a no-op. |
| G4 | high | Bridge portability | telegram_bridge.py hardcodes /home/cbrd21 sys.path; cannot start elsewhere. |
| G5 | high | Wedge detection | Seatbelt catches the known hang but the outer loop retries forever: no failure counter, no exit-to-restart, no sd_notify, no sk-alert. Wedge variants outside `_poll` reproduce the original silent-death incident. |
| G6 | high | Unit correctness / drift | Repo daemon unit is a WatchdogSec=30 kill loop on fresh installs (no sd_notify in code); live units and drop-ins have no reconciliation path back to git, so drift compounds. |
| G7 | high | Health coverage | bridge-supervisor watches only legacy unit names and its own unit exists nowhere; check-health.sh skips the three live services; nothing runs on a schedule; nothing detects wedged-alive. |
| G8 | high | Sovereign TURN | Off-tailnet guests depend on free openrelay.metered.ca because UDP 3478 + relay range are closed publicly. Third party in the flagship media path, no alert on fallback. |
| G9 | medium | HA / SPOF | Single instance of everything on one box (which is at 94% disk). No standby on .41. Direct violation of the redundancy mantra. |
| G10 | medium | coturn supervision | Unit says dead, container says up; recovery semantics split between systemd and Docker restart policy; start script and unit not in repo. |
| G11 | medium | Web serving | Prod Flutter web is a dev `http.server` on 0.0.0.0:8088 serving a mutable checkout dir; the better proxy path (`deploy-web.sh` + `_web_proxy.py`, which also fixes the consent /api 404) has no unit. |
| G12 | medium | Secrets provisioning | Live-path secrets scattered in hand-edited drop-ins and `~/.config/livekit/livekit.yaml` with no provisioning or rotation story; a live drop-in embeds a Postgres DSN inline. SECRETS.md covers only the unused Swarm path. |
| G13 | medium | Nostr durability | In-memory relay: any restart wipes federation SFU discovery until publishers re-announce. |
| G14 | medium | CI coverage of scripts/ | ruff runs on src/ and tests/ only; the 1,100+ line telegram_bridge.py and bridge_consciousness.py are unlinted and their poll/seatbelt/media/tool-loop paths untested. |
| G15 | medium | Live QA legs | e2e-conf-verify.sh, two-browser tests, lane harness: all manual-only, no scheduled runner, no alert on regression. |
| G16 | medium | Bridge throughput | Sequential poll loop with 180s LLM and 120s-per-call tool round-trips: one slow turn freezes all chats and looks like a wedge. |
| G17 | low | Flutter cold-start | pubspec pins sk_pqc to a sibling path, sk_pqc_noble.js build undocumented, STATUS/HANDOFF docs describe the Feb scaffold, 3 stale client copies linger. |
| G18 | low | Disk headroom | .158 root at 94%; flutter builds on the primary box can wedge it. |
| G19 | low | State durability | Bridge context is a process-local deque lost on restart, taxing the aggressive-restart policy the wedge fix needs. Accepted for now (skmemory backfills). |

## 4. Remediation Roadmap

### Phase 0: stop the bleeding (all parallelizable, no dependencies)
- **P0.1** Fix publish.yml gate (G3). Minutes of work, high leverage.
- **P0.2** Fix the repo daemon unit watchdog landmine and dead bridge paths (G6 part 1) so the repo units are at least installable.
- **P0.3** Make telegram_bridge.py portable (G4).
- **P0.4** Add Flutter CI to skchat-app (G2).

### Phase 1: reproducibility (the core)
- **P1.1** Reconcile every live .158 unit, drop-in, and timer into the repo, templated and sanitized, with an idempotent installer (G1). Depends on P0.2 and P0.3 (the units it captures must point at portable code).
- **P1.2** Secrets provisioning: inventory live secret locations, write the skvault-backed provisioning script, rotate the inline PG DSN (G12). Parallel with P1.1.
- **P1.3** Cold-machine runbook + bootstrap verification (G1, G17 partially). Depends on P1.1 and P1.2.

### Phase 2: detection and self-recovery
- **P2.1** Bridge wedge escalation: failure counter, exit-for-restart, sd_notify watchdog pings, sk-alert (G5, closes the G6 watchdog story properly). Depends on P0.3.
- **P2.2** External health probe timer covering every live port, wired to sk-alert; retire/replace the stale bridge-supervisor (G7). Parallel with P2.1.
- **P2.3** coturn supervision reconciliation: one owner (systemd), unit + start script in repo (G10). Parallel.
- **P2.4** Nightly live QA leg on a timer with alerting (G15). Depends on P2.2's alert plumbing.
- **P2.5** CI for scripts/: ruff + targeted tests of the poll loop and seatbelt (G14). Parallel, depends on P0.3.

### Phase 3: sovereignty and redundancy
- **P3.1** Sovereign public TURN: open the media path (or TURN over TCP/TLS 443), demote openrelay to alert-on-use or remove it (G8). Independent.
- **P3.2** Nostr relay persistence (G13). Independent, small.
- **P3.3** Proper web serving unit for the Flutter client: versioned artifact + proxy, kill the dev server (G11). Depends on P0.4 (CI produces the artifact) and P1.1 (unit conventions).
- **P3.4** Warm standby on .41 for bridges/piper/nostr with a failover runbook (G9). Depends on P1.1, P1.2, P3.2 (relay state must survive the move).
- **P3.5** Flutter cold-start docs and stale-copy cleanup (G17). Parallel, low.
- **P3.6** Disk headroom guard for .158 (G18). Parallel, low.

Deferred, tracked but not tasked here: bridge concurrency rework (G16) is a design change best done after P2.1 makes slowness observable and distinguishable from wedges; bridge context durability (G19) is accepted risk while skmemory backfills.

## 5. Task List

Titles below are the canonical task titles (mirrored into coordination). Dependencies reference exact titles.

1. **skchat: remove the || true test bypass in publish.yml** (high) - no deps
2. **skchat: fix repo systemd units (watchdog landmine + dead paths)** (high) - no deps
3. **skchat: make telegram_bridge.py portable (drop hardcoded sys.path)** (high) - no deps
4. **skchat: add CI to skchat-app (analyze + test + build web)** (critical) - no deps
5. **skchat: reconcile live .158 units and drop-ins into the repo with installer** (critical) - deps: 2, 3
6. **skchat: secrets provisioning script + rotate inline PG DSN drop-in** (high) - no deps
7. **skchat: cold-machine deploy runbook with bootstrap verification** (high) - deps: 5, 6
8. **skchat: bridge wedge escalation (failure counter, exit-to-restart, sd_notify, sk-alert)** (high) - deps: 3
9. **skchat: scheduled external health probe covering all live services** (high) - no deps
10. **skchat: reconcile coturn supervision under systemd and commit the unit** (medium) - no deps
11. **skchat: nightly live QA leg on a systemd timer with alerting** (medium) - deps: 9
12. **skchat: lint and test scripts/ in CI (poll loop + seatbelt coverage)** (medium) - deps: 3
13. **skchat: sovereign public TURN path (retire openrelay dependency)** (high) - no deps
14. **skchat: persist nostr relay state across restarts** (medium) - no deps
15. **skchat: proper web-serving unit for the Flutter client (kill the dev server)** (medium) - deps: 4, 5
16. **skchat: warm standby on .41 for bridge/TTS/relay plane with failover runbook** (medium) - deps: 5, 6, 14
17. **skchat: Flutter cold-start docs + stale client copy cleanup** (low) - no deps
18. **skchat: disk headroom guard on .158** (low) - no deps
