#!/usr/bin/env bash
# bridge-supervisor.sh — monitors lumina (and opus) bridge services.
#
# Checks skchat-lumina-bridge.service status and lumina-responses.log
# activity. Restarts the bridge if stalled, sends a desktop notification,
# and logs all actions to ~/.skchat/bridge-supervisor.log.
#
# Usage:
#   bridge-supervisor.sh               # continuous loop (60s interval)
#   bridge-supervisor.sh --check-once  # single check then exit

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────

LOG_DIR="${HOME}/.skchat"
LOG_FILE="${LOG_DIR}/bridge-supervisor.log"
RESPONSE_LOG="${LOG_DIR}/lumina-responses.log"
BRIDGE_SERVICE="skchat-lumina-bridge.service"
STALL_THRESHOLD=600   # 10 minutes in seconds
LOOP_INTERVAL=60

mkdir -p "${LOG_DIR}"

# ── Logging ───────────────────────────────────────────────────────────────────

log() {
    local level="$1"; shift
    local ts
    ts="$(date '+%Y-%m-%dT%H:%M:%S')"
    local msg="[${ts}] [${level}] $*"
    printf '%s\n' "${msg}" | tee -a "${LOG_FILE}"
}

log_info()  { log "INFO " "$@"; }
log_warn()  { log "WARN " "$@"; }
log_error() { log "ERROR" "$@"; }

# ── Checks ────────────────────────────────────────────────────────────────────

check_service_active() {
    systemctl --user is-active --quiet "${BRIDGE_SERVICE}" 2>/dev/null
}

# Returns seconds since the last line in lumina-responses.log, or -1 if
# the log is absent / empty / unparseable.
# Log format: [2024-01-15T14:30:00] [from: sender] [response: preview...]
seconds_since_last_response() {
    if [[ ! -f "${RESPONSE_LOG}" ]]; then
        echo -1
        return
    fi
    local last_line
    last_line="$(tail -n 1 "${RESPONSE_LOG}" 2>/dev/null)"
    if [[ -z "${last_line}" ]]; then
        echo -1
        return
    fi
    local ts
    ts="$(printf '%s' "${last_line}" | grep -oP '(?<=\[)\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?=\])')"
    if [[ -z "${ts}" ]]; then
        echo -1
        return
    fi
    local then now
    then="$(date -d "${ts}" +%s 2>/dev/null)" || { echo -1; return; }
    now="$(date +%s)"
    echo $(( now - then ))
}

# ── Supervisor logic ──────────────────────────────────────────────────────────

run_check() {
    log_info "=== bridge supervisor check ==="

    # 1. Is the service running?
    local service_ok=0
    if check_service_active; then
        service_ok=1
        log_info "${BRIDGE_SERVICE}: ACTIVE"
    else
        log_warn "${BRIDGE_SERVICE}: NOT ACTIVE"
    fi

    # 2. Has it processed a message recently?
    local age
    age="$(seconds_since_last_response)"
    local activity_ok=0

    if [[ "${age}" -eq -1 ]]; then
        # No log entries yet — bridge may have just started; only flag if
        # the service itself is also down.
        log_warn "lumina-responses.log: no entries (bridge has not processed any messages yet)"
        activity_ok="${service_ok}"
    elif [[ "${age}" -le "${STALL_THRESHOLD}" ]]; then
        activity_ok=1
        log_info "Last response: ${age}s ago (within ${STALL_THRESHOLD}s threshold) — OK"
    else
        log_warn "Last response: ${age}s ago — exceeds ${STALL_THRESHOLD}s threshold (STALLED)"
    fi

    # 3. Restart if stalled or down
    if [[ "${service_ok}" -eq 0 ]] || [[ "${activity_ok}" -eq 0 ]]; then
        local reason
        if [[ "${service_ok}" -eq 0 ]]; then
            reason="service not active"
        else
            reason="no activity for ${age}s"
        fi
        log_warn "Triggering restart of ${BRIDGE_SERVICE} (reason: ${reason})"

        # Desktop notification (best-effort — may not be available in headless env)
        notify-send --urgency=normal \
            "SKChat Bridge Supervisor" \
            "Restarting ${BRIDGE_SERVICE}: ${reason}" 2>/dev/null || true

        if systemctl --user restart "${BRIDGE_SERVICE}" 2>&1 | tee -a "${LOG_FILE}"; then
            log_info "${BRIDGE_SERVICE} restarted successfully"
        else
            log_error "Failed to restart ${BRIDGE_SERVICE} — check: journalctl --user -u ${BRIDGE_SERVICE}"
        fi
    else
        log_info "Bridge healthy — no action needed"
    fi

    log_info "=== check complete ==="
}

# ── Entry point ───────────────────────────────────────────────────────────────

CHECK_ONCE=0
for arg in "$@"; do
    case "${arg}" in
        --check-once) CHECK_ONCE=1 ;;
        -h|--help)
            echo "Usage: $(basename "$0") [--check-once]"
            echo "  --check-once  Run a single check and exit (for testing/cron)"
            exit 0
            ;;
        *)
            echo "Unknown argument: ${arg}" >&2
            exit 1
            ;;
    esac
done

if [[ "${CHECK_ONCE}" -eq 1 ]]; then
    run_check
    exit 0
fi

log_info "Bridge supervisor starting (loop interval: ${LOOP_INTERVAL}s, stall threshold: ${STALL_THRESHOLD}s)"
while true; do
    run_check
    sleep "${LOOP_INTERVAL}"
done
