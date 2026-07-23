#!/usr/bin/env bash
# health-probe.sh - SKChat external health probe (pass/fail + alerting)
#
# Extends check-health.sh (which checks local process/config state) into a
# probe of every LIVE network-facing skchat surface, from outside the process
# tree, the way an external caller (the webui, a peer, the public app) would
# see it. Meant to run unattended on a timer (skchat-health-probe.timer).
#
# Surfaces probed (see systemd/README.md + docs/WEBAPP-AND-API-ARCHITECTURE.md
# for the live port map this mirrors):
#   1. skchat daemon health         :9385/health   (JSON status=ok)
#   2. skcomms transport health     :9384/health   (JSON status=ok)
#   3. webui (per-agent, lumina)    :8765          (HTTP reachable)
#   4. coturn TURN relay            Funnel :8443   (TCP reachable)
#   5. livekit SFU                  :7880          (TCP reachable, tailnet-bound)
#   6. nostr discovery relay        :7447          (TCP reachable, tailnet-bound)
#   7. piper TTS                    :18797         (HTTP reachable)
#   8. public HTTPS ingress         noroc2027.tail204f0c.ts.net (HTTP reachable)
#
# Every check is best-effort tolerant (curl --max-time / TCP connect timeout):
# a slow or hung surface reads as RED, it never hangs the probe itself.
#
# Exit code: 0 if every surface is green, 1 if any surface is red (this is
# what drives the systemd timer's on-failure alert path).
#
# Alerting: on any failure, best-effort notifies via sk-alert with the list of
# failing surfaces. sk-alert being absent/broken NEVER changes this script's
# own exit code, alerting is a side effect, not a dependency.

set -uo pipefail

TIMEOUT="${HEALTH_PROBE_TIMEOUT:-5}"

# Ports / hosts (override via env for testing; defaults match live .158).
DAEMON_HEALTH_URL="${SKCHAT_HEALTH_URL:-http://localhost:9385/health}"
SKCOMMS_HEALTH_URL="${SKCOMMS_HEALTH_URL:-http://localhost:9384/health}"
WEBUI_URL="${SKCHAT_WEBUI_URL:-http://localhost:${SKCHAT_PORT:-8765}/}"
COTURN_FUNNEL_HOST="${COTURN_FUNNEL_HOST:-noroc2027.tail204f0c.ts.net}"
COTURN_FUNNEL_PORT="${COTURN_FUNNEL_PORT:-8443}"
LIVEKIT_PORT="${LIVEKIT_PORT:-7880}"
NOSTR_PORT="${NOSTR_PORT:-7447}"
PIPER_URL="${PIPER_URL:-http://localhost:18797/}"
PUBLIC_URL="${SKCHAT_PUBLIC_URL:-https://noroc2027.tail204f0c.ts.net/}"

# livekit + nostr bind the tailnet IP, not loopback (see CLAUDE.md "Systemd
# Services" and livekit_routes.py TAILNET_HOST_ENV). Resolve it the same way
# the app does: explicit env wins, else `tailscale ip -4`, else the known
# live fallback.
TAILNET_HOST="${SKCHAT_TAILNET_HOST:-}"
if [[ -z "$TAILNET_HOST" ]]; then
    TAILNET_HOST="$(command -v tailscale &>/dev/null && tailscale ip -4 2>/dev/null | head -1 || true)"
fi
TAILNET_HOST="${TAILNET_HOST:-100.108.59.57}"

GREEN="\033[0;32m"
RED="\033[0;31m"
RESET="\033[0m"
BOLD="\033[1m"

PASS=0
FAIL=0
FAILED_SURFACES=()

green() { printf "  ${GREEN}GREEN${RESET}  %-28s %s\n" "$1" "$2"; PASS=$((PASS + 1)); }
red()   { printf "  ${RED}RED${RESET}    %-28s %s\n" "$1" "$2"; FAIL=$((FAIL + 1)); FAILED_SURFACES+=("$1"); }

# check_http_json_ok NAME URL - GET URL, GREEN if it returns HTTP 2xx and the
# body contains "status":"ok" (tolerant of whitespace after the colon).
check_http_json_ok() {
    local name="$1" url="$2" body
    body=$(timeout "$TIMEOUT" curl -sf --max-time "$TIMEOUT" "$url" 2>/dev/null || true)
    if [[ -n "$body" ]] && printf '%s' "$body" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"'; then
        green "$name" "$url -> status ok"
    else
        red "$name" "$url -> ${body:-no response}"
    fi
}

# check_http_reachable NAME URL - GET URL, GREEN on any HTTP response at all
# (connection + a status line; the surface has no dedicated health route so a
# live server answering with ANY code, even a 404/405, proves it's up).
check_http_reachable() {
    local name="$1" url="$2" code
    code=$(timeout "$TIMEOUT" curl -s -o /dev/null -w '%{http_code}' --max-time "$TIMEOUT" "$url" 2>/dev/null || true)
    if [[ -n "$code" && "$code" != "000" ]]; then
        green "$name" "$url -> HTTP $code"
    else
        red "$name" "$url -> unreachable"
    fi
}

# check_tcp NAME HOST PORT - plain TCP connect, GREEN if the port accepts a
# connection (used for surfaces with no HTTP semantics worth trusting: TURN,
# the raw SFU/relay ports which are tailnet-bound and websocket-only). Runs
# the connect attempt in a throwaway subshell so the fd closes automatically
# when it exits, win or lose.
check_tcp() {
    local name="$1" host="$2" port="$3"
    if timeout "$TIMEOUT" bash -c "exec 3<>'/dev/tcp/${host}/${port}'" 2>/dev/null; then
        green "$name" "${host}:${port} -> TCP open"
    else
        red "$name" "${host}:${port} -> TCP refused/timeout"
    fi
}

echo ""
printf "${BOLD}SKChat External Health Probe${RESET}\n"
printf "%s\n" "$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
printf "───────────────────────────────────────────────────────\n"

check_http_json_ok "skchat-daemon"    "$DAEMON_HEALTH_URL"
check_http_json_ok "skcomms-transport" "$SKCOMMS_HEALTH_URL"
check_http_reachable "webui-lumina"   "$WEBUI_URL"
check_tcp "coturn-funnel"             "$COTURN_FUNNEL_HOST" "$COTURN_FUNNEL_PORT"
check_tcp "livekit-sfu"               "$TAILNET_HOST" "$LIVEKIT_PORT"
check_tcp "nostr-relay"               "$TAILNET_HOST" "$NOSTR_PORT"
check_http_reachable "piper-tts"      "$PIPER_URL"
check_http_reachable "public-ingress" "$PUBLIC_URL"

echo ""
printf "───────────────────────────────────────────────────────\n"
printf "${BOLD}Summary:${RESET} ${GREEN}%d green${RESET}" "$PASS"
if [[ $FAIL -gt 0 ]]; then
    printf ", ${RED}%d red${RESET}" "$FAIL"
fi
echo ""
echo ""

if [[ $FAIL -gt 0 ]]; then
    # Best-effort alert: never let sk-alert's absence, or a failure inside it,
    # change this script's own exit code. Dedup 30 min so a stuck-down surface
    # doesn't page every 5-minute timer tick.
    surfaces="$(IFS=,; echo "${FAILED_SURFACES[*]}")"
    if command -v sk-alert &>/dev/null; then
        sk-alert -l crit -k health-probe -t 1800 \
            "skchat health-probe: RED surface(s): ${surfaces}" 2>/dev/null || true
    fi
    exit 1
fi

exit 0
