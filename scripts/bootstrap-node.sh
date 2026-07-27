#!/usr/bin/env bash
# bootstrap-node.sh - bring a blank machine into the skchat plane, in order.
#
# Runs the 6-step chain documented in docs/BOOTSTRAP.md:
#   1. ~/.skenv packages
#   2. CapAuth identity / operator PGP key import   <- chicken-and-egg breaker
#   3. skvault unlock
#   4. ~/.skchat restore (or fresh provision) + identity.json/peers manual gap
#   5. systemd/install.sh --enable
#   6. tailscale ingress (systemd/tailscale-ingress.sh, dry-run by default)
#
# Every step preflight-checks its own prerequisites FIRST and fails loudly,
# with a specific fix instruction, rather than half-running. Safe to re-run:
# each step checks whether it is already satisfied and skips with [SKIP] if
# so (idempotent). Nothing here invents secrets or private keys - where a
# step needs material that can only come from an already-provisioned node
# (the operator's PGP key, an agent's CapAuth profile, identity.json/peers),
# the script says so explicitly and stops rather than guessing.
#
# Usage:
#   bootstrap-node.sh --check              preflight only, print a readiness
#                                           report, do NOT run/mutate anything.
#                                           (recommended first run, always)
#   bootstrap-node.sh                      run the ordered steps for real,
#                                           skipping any already satisfied.
#   bootstrap-node.sh --apply-ingress      also apply step 6 for real (default
#                                           is --dry-run; see the safety note
#                                           in docs/BOOTSTRAP.md step 6).
#   bootstrap-node.sh --skip-ingress       skip step 6 entirely.
#   bootstrap-node.sh --force-restore      allow step 4 to restore over an
#                                           already-populated ~/.skchat
#                                           (default: skip if already present).
#
# Options:
#   --agent NAME             Agent to bootstrap (default: $SKAGENT, else lumina).
#   --home DIR                Base home dir (default: $HOME). For a real rebuild
#                             this stays $HOME; override only for a scratch test.
#   --recipient EMAIL         PGP recipient/UID to check for in the gpg keyring
#                             (default: $CAPAUTH_PGP_RECIPIENT, else
#                             $SKINGEST_PGP_RECIPIENT, else chef@skworld.io).
#   --backup-archive PATH     Explicit backup archive for step 4 (default:
#                             latest ~/.skchat-backups/skchat-*.tar.gz.pgp).
#
# Exit codes: 0 = all good (in --check: no blocking prerequisite missing).
#             1 = one or more blocking prerequisites missing / step failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

CHECK_ONLY=0
APPLY_INGRESS=0
SKIP_INGRESS=0
FORCE_RESTORE=0
AGENT="${SKAGENT:-${SKCAPSTONE_AGENT:-lumina}}"
HOME_DIR="${HOME}"
RECIPIENT="${CAPAUTH_PGP_RECIPIENT:-${SKINGEST_PGP_RECIPIENT:-chef@skworld.io}}"
BACKUP_ARCHIVE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check)          CHECK_ONLY=1; shift ;;
        --apply-ingress)  APPLY_INGRESS=1; shift ;;
        --skip-ingress)   SKIP_INGRESS=1; shift ;;
        --force-restore)  FORCE_RESTORE=1; shift ;;
        --agent)          AGENT="$2"; shift 2 ;;
        --home)           HOME_DIR="$2"; shift 2 ;;
        --recipient)      RECIPIENT="$2"; shift 2 ;;
        --backup-archive) BACKUP_ARCHIVE="$2"; shift 2 ;;
        -h|--help) sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "error: unknown option: $1" >&2; exit 1 ;;
    esac
done

SKENV="${HOME_DIR}/.skenv"
SKCAPSTONE_HOME="${HOME_DIR}/.skcapstone"
AGENT_CAPAUTH_DIR="${SKCAPSTONE_HOME}/agents/${AGENT}/capauth/identity"
LEGACY_CAPAUTH_DIR="${SKCAPSTONE_HOME}/capauth/identity"
BACKUP_DIR="${HOME_DIR}/.skchat-backups"
SHARED_IDENTITY="${SKCAPSTONE_HOME}/identity/identity.json"
PEERS_DIR="${HOME_DIR}/.skcomms/peers"

BLOCKING=0
WARNINGS=0

hdr()  { echo ""; echo "[$1] $2"; }
ok()   { echo "  [OK]   $*"; }
info() { echo "  [INFO] $*"; }
warn() { echo "  [WARN] $*"; WARNINGS=$((WARNINGS + 1)); }
miss() { echo "  [MISS] $*"; BLOCKING=$((BLOCKING + 1)); }
skip() { echo "  [SKIP] $*"; }

echo "skchat bootstrap-node: $([[ $CHECK_ONLY -eq 1 ]] && echo 'preflight check' || echo 'run') (agent=${AGENT}, home=${HOME_DIR})"

# ---------------------------------------------------------------------------
# Step 1 preflight: ~/.skenv packages
# ---------------------------------------------------------------------------
preflight_step1() {
    hdr 1/6 "~/.skenv packages"
    if [[ ! -d "$SKENV" ]]; then
        miss "~/.skenv not found -- run: bash <skcapstone-repo>/scripts/install.sh"
        return
    fi
    local all_ok=1
    for bin in skchat skcapstone capauth skvault; do
        if [[ -x "${SKENV}/bin/${bin}" ]]; then
            ok "${bin} -> ${SKENV}/bin/${bin}"
        else
            miss "${SKENV}/bin/${bin} not found -- (re)run skcapstone's scripts/install.sh, then this repo's 'pip install -e .[cli]'"
            all_ok=0
        fi
    done
    [[ $all_ok -eq 1 ]] && ok "~/.skenv fully provisioned"
}

# ---------------------------------------------------------------------------
# Step 2 preflight: CapAuth identity / operator PGP key import
#   (the chicken-and-egg breaker -- see docs/BOOTSTRAP.md Step 2)
# ---------------------------------------------------------------------------
GPG_KEY_PRESENT=0
AGENT_PROFILE_PRESENT=0
preflight_step2() {
    hdr 2/6 "CapAuth identity / PGP key import (chicken-and-egg breaker)"
    if ! command -v gpg >/dev/null 2>&1; then
        miss "gpg not found in PATH -- required before anything else in this step"
        return
    fi
    if gpg --batch --list-secret-keys "$RECIPIENT" >/dev/null 2>&1; then
        ok "operator gpg secret key present for ${RECIPIENT} (breaks the skvault chicken-and-egg)"
        GPG_KEY_PRESENT=1
    else
        miss "no gpg SECRET key for '${RECIPIENT}' in the local keyring. skvault unlock" \
             "cannot work until this is imported -- see docs/BOOTSTRAP.md Step 2" \
             "('gpg --export-secret-keys' on a known-good node -> out-of-band transfer -> 'gpg --import' here)."
    fi
    local prof_dir=""
    if [[ -f "${AGENT_CAPAUTH_DIR}/private.asc" && -f "${AGENT_CAPAUTH_DIR}/profile.json" ]]; then
        prof_dir="$AGENT_CAPAUTH_DIR"
    elif [[ -f "${LEGACY_CAPAUTH_DIR}/private.asc" && -f "${LEGACY_CAPAUTH_DIR}/profile.json" ]]; then
        prof_dir="$LEGACY_CAPAUTH_DIR"
    fi
    if [[ -n "$prof_dir" ]]; then
        ok "agent '${AGENT}' CapAuth profile present: ${prof_dir}"
        AGENT_PROFILE_PRESENT=1
    else
        miss "no CapAuth profile for agent '${AGENT}' at ${AGENT_CAPAUTH_DIR} (or legacy ${LEGACY_CAPAUTH_DIR})." \
             "Sync/copy it from a known-good node (Syncthing, or an out-of-band transfer -- this is" \
             "private key material, same rule as the operator key above). Do NOT run 'capauth init'" \
             "for an agent that already has an identity elsewhere -- that mints a NEW, different identity."
    fi
}

# ---------------------------------------------------------------------------
# Step 3 preflight: skvault unlock (read-only probe, never mutates)
# ---------------------------------------------------------------------------
VAULT_UNLOCKED=0
preflight_step3() {
    hdr 3/6 "skvault unlock"
    if [[ ! -x "${SKENV}/bin/skvault" ]]; then
        miss "skvault shim not found (depends on step 1)"
        return
    fi
    if [[ $GPG_KEY_PRESENT -ne 1 ]]; then
        warn "skipping live unlock probe: step 2's gpg key is not present yet, unlock would fail"
        return
    fi
    local line
    line="$("${SKENV}/bin/skvault" status 2>/dev/null || true)"
    if echo "$line" | grep -qi "unlocked"; then
        ok "skvault already unlocked: ${line}"
        VAULT_UNLOCKED=1
    else
        warn "skvault is locked: ${line:-"(no status output)"} -- would run 'skvault unlock' (interactive passphrase prompt)"
    fi
}

# ---------------------------------------------------------------------------
# Step 4 preflight: ~/.skchat restore/provision + identity.json/peers gap
# ---------------------------------------------------------------------------
LATEST_ARCHIVE=""
preflight_step4() {
    hdr 4/6 "~/.skchat restore or fresh provision"
    local skchat_dir="${HOME_DIR}/.skchat"
    if [[ -d "$skchat_dir" ]] && find "$skchat_dir" -mindepth 1 -print -quit 2>/dev/null | grep -q .; then
        ok "~/.skchat already present ($(find "$skchat_dir" -mindepth 1 -maxdepth 1 | wc -l) top-level items) -- restore step would skip"
    else
        info "~/.skchat is absent or empty -- restore or fresh-provision would run"
    fi
    if [[ -n "$BACKUP_ARCHIVE" ]]; then
        if [[ -f "$BACKUP_ARCHIVE" ]]; then
            ok "explicit --backup-archive found: ${BACKUP_ARCHIVE}"
            LATEST_ARCHIVE="$BACKUP_ARCHIVE"
        else
            miss "--backup-archive given but not found: ${BACKUP_ARCHIVE}"
        fi
    elif [[ -d "$BACKUP_DIR" ]]; then
        LATEST_ARCHIVE="$(ls -t "${BACKUP_DIR}"/skchat-*.tar.gz.pgp 2>/dev/null | head -1 || true)"
        if [[ -n "$LATEST_ARCHIVE" ]]; then
            ok "latest backup archive: ${LATEST_ARCHIVE}"
        else
            info "no backup archive under ${BACKUP_DIR} -- fresh provision (scripts/bootstrap.sh) would run instead"
        fi
    else
        info "no ${BACKUP_DIR} at all -- fresh provision (scripts/bootstrap.sh) would run instead"
    fi
    if [[ -f "$SHARED_IDENTITY" ]]; then
        ok "~/.skcapstone/identity/identity.json present (operator identity; NOT covered by the Task-1 backup)"
    else
        warn "DR GAP: ${SHARED_IDENTITY} missing -- skchat's at-rest DEK derivation" \
             "(encrypted_store.py) needs this. Manual step: rsync it from a known-good node." \
             "See docs/BOOTSTRAP.md Step 4."
    fi
    if [[ -d "$PEERS_DIR" ]] && find "$PEERS_DIR" -mindepth 1 -print -quit 2>/dev/null | grep -q .; then
        ok "~/.skcomms/peers present (peer registry; NOT covered by the Task-1 backup)"
    else
        warn "DR GAP: ${PEERS_DIR} missing/empty -- peer trust registry not covered by the Task-1" \
             "backup either. Manual step: rsync it from a known-good node, or fall back to" \
             "scripts/seed-peers.py / scripts/generate-peers-from-agents.py (weaker trust state)."
    fi
}

# ---------------------------------------------------------------------------
# Step 5 preflight: systemd/install.sh --enable
# ---------------------------------------------------------------------------
preflight_step5() {
    hdr 5/6 "systemd/install.sh --enable"
    if [[ ! -x "${REPO_ROOT}/systemd/install.sh" ]]; then
        miss "${REPO_ROOT}/systemd/install.sh not found or not executable"
        return
    fi
    ok "systemd/install.sh found"
    if command -v systemd-analyze >/dev/null 2>&1; then
        ok "systemd-analyze available (install.sh verifies every unit before daemon-reload)"
    else
        warn "systemd-analyze not found -- install.sh's unit-verify step will report [WARN] for every unit"
    fi
    if command -v systemctl >/dev/null 2>&1 && systemctl --user is-enabled skchat-daemon.service >/dev/null 2>&1; then
        ok "skchat-daemon.service already enabled on this host"
    else
        info "skchat-daemon.service not currently enabled -- 'install.sh --enable' would enable the live-set"
    fi
}

# ---------------------------------------------------------------------------
# Step 6 preflight: tailscale ingress
# ---------------------------------------------------------------------------
preflight_step6() {
    hdr 6/6 "tailscale ingress (systemd/tailscale-ingress.sh)"
    if [[ $SKIP_INGRESS -eq 1 ]]; then
        skip "--skip-ingress given"
        return
    fi
    if ! command -v tailscale >/dev/null 2>&1; then
        miss "tailscale not found in PATH"
    else
        ok "tailscale found: $(command -v tailscale)"
    fi
    if ! command -v jq >/dev/null 2>&1; then
        miss "jq not found in PATH (required by tailscale-ingress.sh)"
    else
        ok "jq found: $(command -v jq)"
    fi
    if [[ -x "${REPO_ROOT}/systemd/tailscale-ingress.sh" ]]; then
        ok "systemd/tailscale-ingress.sh found"
        if [[ $APPLY_INGRESS -eq 1 ]]; then
            warn "--apply-ingress given: this step would apply for REAL against shared live ingress"
        else
            info "would run in --dry-run mode by default (pass --apply-ingress to apply for real)"
        fi
    else
        miss "${REPO_ROOT}/systemd/tailscale-ingress.sh not found or not executable"
    fi
}

run_preflight_report() {
    preflight_step1
    preflight_step2
    preflight_step3
    preflight_step4
    preflight_step5
    preflight_step6
    echo ""
    echo "Summary: ${BLOCKING} blocking issue(s), ${WARNINGS} warning(s)."
}

if [[ $CHECK_ONLY -eq 1 ]]; then
    run_preflight_report
    echo ""
    echo "(--check: nothing was executed, no mutating command was run.)"
    exit $([[ $BLOCKING -eq 0 ]] && echo 0 || echo 1)
fi

# ---------------------------------------------------------------------------
# Real run: preflight first (fail early), then execute each step, skipping
# anything already satisfied.
# ---------------------------------------------------------------------------
run_preflight_report
if [[ $BLOCKING -gt 0 ]]; then
    echo ""
    echo "FATAL: ${BLOCKING} blocking prerequisite(s) missing (see [MISS] lines above)." >&2
    echo "Fix those first, then re-run (this script is idempotent -- safe to re-run)." >&2
    exit 1
fi

echo ""
echo "Preflight clean. Executing steps..."

# Step 1: nothing to execute -- preflight already confirmed ~/.skenv is provisioned
# (a missing ~/.skenv is a blocking preflight failure above; this script does not
# install a separate repo's package set on your behalf).
echo ""
echo "[1/6] ~/.skenv packages: already satisfied (preflight above), skipping."

# Step 2: nothing to execute -- the key import is an out-of-band, human/operator
# action by design (see docs/BOOTSTRAP.md). Preflight already confirmed both
# pieces are present, or this run would already have failed above.
echo ""
echo "[2/6] CapAuth identity: already satisfied (preflight above), skipping."

# Step 3: unlock skvault if not already unlocked.
echo ""
echo "[3/6] skvault unlock:"
if [[ $VAULT_UNLOCKED -eq 1 ]]; then
    echo "  [SKIP] already unlocked."
else
    "${SKENV}/bin/skvault" unlock
    "${SKENV}/bin/skvault" status
fi

# Step 4: restore or fresh-provision ~/.skchat.
echo ""
echo "[4/6] ~/.skchat restore / provision:"
skchat_dir="${HOME_DIR}/.skchat"
if [[ -d "$skchat_dir" ]] && find "$skchat_dir" -mindepth 1 -print -quit 2>/dev/null | grep -q . && [[ $FORCE_RESTORE -ne 1 ]]; then
    echo "  [SKIP] ~/.skchat already present (pass --force-restore to overwrite from backup)."
elif [[ -n "$LATEST_ARCHIVE" ]]; then
    echo "  restoring from ${LATEST_ARCHIVE} ..."
    bash "${REPO_ROOT}/scripts/restore-skchat.sh" --target "$HOME_DIR" "$LATEST_ARCHIVE"
else
    echo "  no backup archive available -- fresh-provisioning via scripts/bootstrap.sh ..."
    bash "${REPO_ROOT}/scripts/bootstrap.sh"
fi
if [[ ! -f "$SHARED_IDENTITY" || ! -d "$PEERS_DIR" ]]; then
    echo "  [MANUAL STEP REQUIRED] identity.json / peers gap not auto-resolved -- see docs/BOOTSTRAP.md Step 4."
fi

# Step 5: systemd/install.sh --enable (idempotent: copies only on diff, never restarts).
echo ""
echo "[5/6] systemd/install.sh --enable:"
bash "${REPO_ROOT}/systemd/install.sh" --enable

# Step 6: tailscale ingress.
echo ""
echo "[6/6] tailscale ingress:"
if [[ $SKIP_INGRESS -eq 1 ]]; then
    echo "  [SKIP] --skip-ingress given."
elif [[ $APPLY_INGRESS -eq 1 ]]; then
    bash "${REPO_ROOT}/systemd/tailscale-ingress.sh"
else
    echo "  applying in --dry-run mode (pass --apply-ingress to apply for real; see docs/BOOTSTRAP.md Step 6):"
    bash "${REPO_ROOT}/systemd/tailscale-ingress.sh" --dry-run
fi

echo ""
echo "bootstrap-node.sh: done."
