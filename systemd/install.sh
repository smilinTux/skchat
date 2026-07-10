#!/usr/bin/env bash
# install.sh: reconcile the skchat systemd user plane from this repo.
#
# Materializes every live .158 unit, drop-in, timer, the coturn start script,
# and the webui register hook into ~/.config/systemd/user (and a couple of
# %h/.config helper locations), idempotently. Safe to re-run: it copies only
# when content differs and NEVER restarts a running unit (copy + daemon-reload
# only; enable/start are opt-in flags). Templated units carry no absolute paths
# or usernames: systemd resolves %h/%i at runtime.
#
# UNITS AND DROP-INS INSTALLED (the full reconciled .158 skchat plane):
#   Daemons:   skchat-daemon (lumina), skchat-daemon-opus, skchat-daemon-chef
#   Bridges:   skchat-telegram-opus, skchat-telegram-lumina
#              (+ skchat-telegram@.service go-forward template, installed but
#               not enabled; see systemd/README.md)
#   Voice:     skchat-lumina-call, skchat-webui@ (instance @lumina on .158),
#              skchat-piper-tts (:18797), livekit-server, skchat-coturn
#   Discovery: skchat-nostr-relay (:7447)
#   Web:       skchat-app-web (:8088)
#   Agents:    jarvis-heartbeat
#   Timers:    telegram-catchup.timer + telegram-catchup.service
#   Drop-ins:  skchat-daemon.d/{guest,group,dm-ratchet}, skchat-daemon-opus.d/group,
#              skchat-telegram-{opus,lumina}.d/override,
#              skchat-lumina-call.d/{fixes,musetalk,tts,vad,webui},
#              skchat-nostr-relay.d/override, skchat-webui@lumina.d/guest,
#              livekit-server.d/wait-tailnet
#   Helpers:   coturn/start-coturn.sh -> ~/.skchat/coturn/,
#              examples/register_webui.py -> ~/.config/skchat/ (never clobbered)
#
# NOT installed (by design):
#   piper-tts.service (:15090, legacy uvicorn) --- DEPRECATED, superseded by
#     skchat-piper-tts.service (:18797). See systemd/README.md.
#
# ENABLED by default: the live-enabled set only. skchat-daemon-chef and
# telegram-catchup are shipped but left disabled (they are disabled on .158).
#
# Usage:
#   ./systemd/install.sh                 install + daemon-reload (no enable/start)
#   ./systemd/install.sh --dry-run       print planned actions, touch nothing
#   ./systemd/install.sh --diff          show drift: repo vs installed, touch nothing
#   ./systemd/install.sh --enable        also `systemctl --user enable` the live set
#   ./systemd/install.sh --enable --start  also start units that are not running
#                                          (never restarts a running unit)
#
# SAFETY: do NOT run the real install against the live .158 plane as part of an
# automated task. Test on a spare host (e.g. .41) with --dry-run / --diff first.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
SKCHAT_CFG="${HOME}/.config/skchat"
COTURN_DIR="${HOME}/.skchat/coturn"

MODE="install"   # install | dry-run | diff
ENABLE=0
START=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) MODE="dry-run"; shift ;;
        --diff)    MODE="diff"; shift ;;
        --enable)  ENABLE=1; shift ;;
        --start)   START=1; shift ;;
        -h|--help) sed -n '2,60p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "error: unknown option: $1" >&2; exit 1 ;;
    esac
done

# Unit files (units/<name> -> ~/.config/systemd/user/<name>)
UNITS=(
    skchat-daemon.service
    skchat-daemon-opus.service
    skchat-daemon-chef.service
    skchat-app-web.service
    skchat-telegram-opus.service
    skchat-telegram-lumina.service
    skchat-telegram@.service
    skchat-lumina-call.service
    skchat-nostr-relay.service
    skchat-piper-tts.service
    skchat-webui@.service
    livekit-server.service
    jarvis-heartbeat.service
    skchat-coturn.service
    telegram-catchup.service
    telegram-catchup.timer
)

# Units to enable by default (matches the .158 live-enabled set). Excludes
# skchat-daemon-chef (disabled on .158), telegram-catchup.service (static;
# enable the .timer instead), and the skchat-telegram@ template.
ENABLE_UNITS=(
    skchat-daemon.service
    skchat-daemon-opus.service
    skchat-app-web.service
    skchat-telegram-opus.service
    skchat-telegram-lumina.service
    skchat-lumina-call.service
    skchat-nostr-relay.service
    skchat-piper-tts.service
    skchat-webui@lumina.service
    livekit-server.service
    jarvis-heartbeat.service
    skchat-coturn.service
    telegram-catchup.timer
)

# Required secret-bearing EnvironmentFiles (name -> description). Referenced with
# `-` (optional) in the units so a missing file never takes a service down, but
# the service degrades without it, so we preflight and warn.
REQUIRED_ENV=(
    "${SKCHAT_CFG}/guest.env|guest-link token (SKCHAT_GUEST_TOKEN_SECRET)"
    "${SKCHAT_CFG}/memory-pg.env|skmem-pg DSN for the telegram bridges (SKMEMORY_PG_DSN)"
    "${SKCHAT_CFG}/telegram-opus.env|Opus bot token"
    "${SKCHAT_CFG}/telegram-lumina.env|Lumina bot token"
    "${SKCHAT_CFG}/webui-lumina.env|lumina webui config + LiveKit secret (SKCHAT_PORT required)"
    "${HOME}/.config/livekit/livekit.yaml|LiveKit API keys"
    "${COTURN_DIR}/coturn.secret|coturn shared secret"
)

say()  { echo "$@"; }
info() { echo "  $@"; }

# copy_file SRC DST [MODE] --- honors MODE=dry-run/diff, reports status.
copy_file() {
    local src="$1" dst="$2" fmode="${3:-0644}"
    if [[ ! -f "$src" ]]; then
        info "[SKIP] $(basename "$dst") (source missing: $src)"
        return 0
    fi
    if [[ "$MODE" == "diff" ]]; then
        if [[ ! -f "$dst" ]]; then
            info "[NEW]  ${dst#"$HOME"/} (not installed)"
        elif ! cmp -s "$src" "$dst"; then
            info "[DRIFT] ${dst#"$HOME"/}"
            diff -u "$dst" "$src" | sed 's/^/    /' || true
        else
            info "[SAME] ${dst#"$HOME"/}"
        fi
        return 0
    fi
    if [[ "$MODE" == "dry-run" ]]; then
        if [[ ! -f "$dst" ]]; then
            info "[+NEW] ${dst#"$HOME"/}"
        elif ! cmp -s "$src" "$dst"; then
            info "[+UPD] ${dst#"$HOME"/}"
        else
            info "[=OK]  ${dst#"$HOME"/}"
        fi
        return 0
    fi
    # real install
    mkdir -p "$(dirname "$dst")"
    if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
        info "[=OK]  ${dst#"$HOME"/}"
    else
        install -m "$fmode" "$src" "$dst"
        info "[WROTE] ${dst#"$HOME"/}"
    fi
}

say "skchat systemd reconcile (mode: ${MODE})"
say "  unit dir: ${UNIT_DIR}"
say ""

# 1. Units
say "Units:"
for u in "${UNITS[@]}"; do
    copy_file "${SCRIPT_DIR}/units/${u}" "${UNIT_DIR}/${u}"
done

# 2. Drop-ins (mirror systemd/dropins/<unit>.d/*.conf)
say ""
say "Drop-ins:"
if [[ -d "${SCRIPT_DIR}/dropins" ]]; then
    while IFS= read -r -d '' conf; do
        rel="${conf#"${SCRIPT_DIR}/dropins/"}"
        copy_file "$conf" "${UNIT_DIR}/${rel}"
    done < <(find "${SCRIPT_DIR}/dropins" -type f -name '*.conf' -print0 | sort -z)
fi

# 3. Helper files (not under ~/.config/systemd/user)
say ""
say "Helpers:"
copy_file "${SCRIPT_DIR}/coturn/start-coturn.sh" "${COTURN_DIR}/start-coturn.sh" 0755
# register_webui.py: install only if absent (never clobber a live-tuned copy).
if [[ -f "${SKCHAT_CFG}/register_webui.py" ]]; then
    info "[KEEP] ${SKCHAT_CFG#"$HOME"/}/register_webui.py (exists, not overwritten)"
elif [[ "$MODE" == "diff" || "$MODE" == "dry-run" ]]; then
    info "[+NEW] ${SKCHAT_CFG#"$HOME"/}/register_webui.py (would install)"
else
    mkdir -p "${SKCHAT_CFG}"
    install -m 0644 "${SCRIPT_DIR}/examples/register_webui.py" "${SKCHAT_CFG}/register_webui.py"
    info "[WROTE] ${SKCHAT_CFG#"$HOME"/}/register_webui.py"
fi

# 4. Secret preflight
say ""
say "Secret preflight (required EnvironmentFiles):"
missing=0
for entry in "${REQUIRED_ENV[@]}"; do
    path="${entry%%|*}"; desc="${entry#*|}"
    if [[ -f "$path" ]]; then
        info "[OK]   ${path#"$HOME"/}"
    else
        info "[MISS] ${path#"$HOME"/}  <- ${desc}"
        missing=$((missing + 1))
    fi
done
if [[ $missing -gt 0 ]]; then
    say ""
    say "  WARNING: ${missing} secret file(s) missing. The referencing services will"
    say "  start but degrade (guest links off, memory recall off, bridges tokenless,"
    say "  livekit/coturn down) until you provision them. Templates: systemd/examples/."
fi

if [[ "$MODE" == "diff" || "$MODE" == "dry-run" ]]; then
    say ""
    say "(${MODE}: nothing was written.)"
    exit 0
fi

# 5. Verify rendered unit files (read-only, safe).
say ""
say "Verifying units (systemd-analyze --user verify):"
verify_fail=0
for u in "${UNITS[@]}"; do
    src="${SCRIPT_DIR}/units/${u}"
    [[ -f "$src" ]] || continue
    # Templates (@.service) are verified via a throwaway instance so %i resolves.
    if [[ "$u" == *@.service ]]; then
        tmp="${UNIT_DIR}/${u/@./@verifyinstance.}"
        cp "$src" "$tmp"
        if systemd-analyze --user verify "$tmp" 2>/dev/null; then
            info "[PASS] ${u}"
        else
            info "[WARN] ${u} (verify reported issues)"; verify_fail=$((verify_fail+1))
        fi
        rm -f "$tmp"
    else
        if systemd-analyze --user verify "${UNIT_DIR}/${u}" 2>/dev/null; then
            info "[PASS] ${u}"
        else
            info "[WARN] ${u} (verify reported issues)"; verify_fail=$((verify_fail+1))
        fi
    fi
done

# 6. daemon-reload (safe; does not restart running units).
say ""
say "Reloading systemd user daemon..."
systemctl --user daemon-reload

# 7. Optional enable (idempotent).
if [[ $ENABLE -eq 1 ]]; then
    say ""
    say "Enabling live-set units:"
    for u in "${ENABLE_UNITS[@]}"; do
        systemctl --user enable "$u" >/dev/null 2>&1 && info "[EN] $u" || info "[EN?] $u (enable skipped/failed)"
    done
fi

# 8. Optional start (only if not already active; never restarts).
if [[ $START -eq 1 ]]; then
    say ""
    say "Starting inactive live-set units (running units are left untouched):"
    for u in "${ENABLE_UNITS[@]}"; do
        if systemctl --user is-active --quiet "$u"; then
            info "[RUN] $u (already active, left as-is)"
        else
            systemctl --user start "$u" && info "[START] $u" || info "[ERR] $u failed to start"
        fi
    done
fi

say ""
say "Done (mode: ${MODE}). Nothing was restarted."
[[ $verify_fail -eq 0 ]] || say "Note: ${verify_fail} unit(s) produced verify warnings above."
