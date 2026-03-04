#!/usr/bin/env bash
# check-health.sh — SKChat system health check
#
# Checks:
#   1. Config file  (~/.skchat/config.yml)
#   2. SKChat daemon running  (PID file + live process)
#   3. Peers configured  (~/.skcapstone/peers/lumina.json + claude.json)
#   4. MCP server responds  (skchat-mcp stdin probe)
#   5. Lumina bridge running  (lumina-bridge.py process)
#
# Output: GREEN ✓ for pass, RED ✗ for fail, YELLOW ~ for warning.
# Exit code: 0 if all hard checks pass, 1 if any fail.

set -uo pipefail

SKCHAT_HOME="${SKCHAT_HOME:-$HOME/.skchat}"
SKCAPSTONE_HOME="${SKCAPSTONE_HOME:-$HOME/.skcapstone}"
LUMINA_SCRIPT="lumina-bridge.py"

GREEN="\033[0;32m"
RED="\033[0;31m"
YELLOW="\033[0;33m"
RESET="\033[0m"
BOLD="\033[1m"

PASS=0
FAIL=0

pass() { printf "  ${GREEN}✓${RESET}  %s\n" "$1"; PASS=$((PASS + 1)); }
fail() { printf "  ${RED}✗${RESET}  %s\n" "$1"; FAIL=$((FAIL + 1)); }
warn() { printf "  ${YELLOW}~${RESET}  %s\n" "$1"; }

echo ""
printf "${BOLD}SKChat Health Check${RESET}\n"
printf "%s\n" "$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
printf "─────────────────────────────────────────\n"

# ─── 1. Config file ──────────────────────────────────────────────

printf "\n${BOLD}1. Config file${RESET}\n"

CONFIG_FILE="${SKCHAT_HOME}/config.yml"
if [[ -f "$CONFIG_FILE" ]]; then
    pass "Config exists: ${CONFIG_FILE}"
else
    fail "Config missing: ${CONFIG_FILE}"
    printf "     Create:  skchat config init  (or copy from docs/config.example.yml)\n"
fi

# ─── 2. SKChat daemon ────────────────────────────────────────────

printf "\n${BOLD}2. SKChat daemon${RESET}\n"

PID_FILE="${SKCHAT_HOME}/daemon.pid"
if [[ -f "$PID_FILE" ]]; then
    daemon_pid=$(cat "$PID_FILE" 2>/dev/null || echo "")
    if [[ -n "$daemon_pid" ]] && kill -0 "$daemon_pid" 2>/dev/null; then
        pass "Daemon running (PID ${daemon_pid})"
    else
        fail "PID file exists but process is not running (stale PID: ${daemon_pid:-?})"
        printf "     Run:  skchat daemon start\n"
    fi
else
    fail "Daemon not running — no PID file at ${PID_FILE}"
    printf "     Run:  skchat daemon start\n"
fi

# ─── 3. Peers configured ─────────────────────────────────────────

printf "\n${BOLD}3. Peers configured${RESET}\n"

PEERS_DIR="${SKCAPSTONE_HOME}/peers"
if [[ -d "$PEERS_DIR" ]]; then
    pass "Peers directory exists: ${PEERS_DIR}"
    for peer in lumina claude; do
        peer_file="${PEERS_DIR}/${peer}.json"
        if [[ -f "$peer_file" ]]; then
            pass "Peer '${peer}' configured: ${peer_file}"
        else
            fail "Peer '${peer}' missing: ${peer_file}"
        fi
    done
else
    fail "Peers directory missing: ${PEERS_DIR}"
    printf "     Create:  mkdir -p ${PEERS_DIR}\n"
fi

# ─── 4. MCP server ───────────────────────────────────────────────

printf "\n${BOLD}4. MCP server (skchat-mcp)${RESET}\n"

if command -v skchat-mcp &>/dev/null; then
    # Send a minimal JSON-RPC initialize request; look for a valid response line
    mcp_probe='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"health-check","version":"1.0"}}}'
    mcp_response=$(printf '%s\n' "$mcp_probe" | timeout 5 skchat-mcp 2>/dev/null | head -1 || echo "")
    if printf '%s' "$mcp_response" | grep -q '"result"'; then
        pass "MCP server responds to initialize"
    else
        warn "MCP server did not return expected response (may need daemon running)"
        printf "     Response: %s\n" "${mcp_response:-(empty)}"
    fi
else
    fail "skchat-mcp not found — run: pip install -e '.[cli]'"
fi

# ─── 5. Lumina bridge ────────────────────────────────────────────

printf "\n${BOLD}5. Lumina bridge${RESET}\n"

lumina_pid=$(pgrep -f "$LUMINA_SCRIPT" 2>/dev/null || true)
if [[ -n "$lumina_pid" ]]; then
    pass "Lumina bridge running (PID ${lumina_pid})"
else
    warn "Lumina bridge not running (optional component)"
    printf "     Start:  python3 scripts/lumina-bridge.py &\n"
fi

# ─── Summary ─────────────────────────────────────────────────────

echo ""
printf "─────────────────────────────────────────\n"
printf "${BOLD}Summary:${RESET} ${GREEN}%d passed${RESET}" "$PASS"
if [[ $FAIL -gt 0 ]]; then
    printf ", ${RED}%d failed${RESET}" "$FAIL"
fi
echo ""
echo ""

if [[ $FAIL -gt 0 ]]; then
    exit 1
fi

exit 0
