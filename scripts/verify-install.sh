#!/usr/bin/env bash
# verify-install.sh — SKChat installation verification
#
# Checks:
#   1. skchat binary exists and prints version
#   2. skchat-mcp binary exists
#   3. ~/.skchat/ directory exists
#   4. ~/.skcapstone/peers/lumina.json exists
#   5. skchat daemon is running (PID file + live process)
#   6. Lumina bridge is running (systemd status)
#   7. MCP config (~/.claude/settings.json) has skchat entry
#   8. skchat inbox --limit 1 returns a response
#
# Exit 0 if all checks pass, 1 if any fail.

set -uo pipefail

SKCHAT_HOME="${SKCHAT_HOME:-$HOME/.skchat}"
SKCAPSTONE_HOME="${SKCAPSTONE_HOME:-$HOME/.skcapstone}"
MCP_CONFIG="${MCP_CONFIG:-$HOME/.claude/settings.json}"

GREEN="\033[0;32m"
RED="\033[0;31m"
RESET="\033[0m"
BOLD="\033[1m"

FAIL=0

pass() { printf "  ${GREEN}✓ PASS${RESET}  %s\n" "$1"; }
fail() { printf "  ${RED}✗ FAIL${RESET}  %s\n" "$1"; FAIL=$((FAIL + 1)); }

echo ""
printf "${BOLD}SKChat Install Verification${RESET}\n"
printf "%s\n" "$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
printf "─────────────────────────────────────────────\n"

# ─── 1. skchat binary ─────────────────────────────────────────────────────────

printf "\n${BOLD}1. skchat binary${RESET}\n"

if command -v skchat &>/dev/null; then
    skchat_ver=$(skchat --version 2>&1 | head -1 || echo "(version unavailable)")
    pass "skchat found: $(command -v skchat) — ${skchat_ver}"
else
    fail "skchat not found in PATH — run: pip install -e '.[cli]'"
fi

# ─── 2. skchat-mcp binary ─────────────────────────────────────────────────────

printf "\n${BOLD}2. skchat-mcp binary${RESET}\n"

if command -v skchat-mcp &>/dev/null; then
    pass "skchat-mcp found: $(command -v skchat-mcp)"
else
    fail "skchat-mcp not found in PATH — run: pip install -e '.[cli]'"
fi

# ─── 3. ~/.skchat/ directory ──────────────────────────────────────────────────

printf "\n${BOLD}3. ~/.skchat/ directory${RESET}\n"

if [[ -d "${SKCHAT_HOME}" ]]; then
    pass "~/.skchat/ exists: ${SKCHAT_HOME}"
else
    fail "~/.skchat/ missing: ${SKCHAT_HOME} — run: mkdir -p ${SKCHAT_HOME}"
fi

# ─── 4. lumina.json peer file ─────────────────────────────────────────────────

printf "\n${BOLD}4. Lumina peer config${RESET}\n"

LUMINA_PEER="${SKCAPSTONE_HOME}/peers/lumina.json"
if [[ -f "${LUMINA_PEER}" ]]; then
    pass "lumina.json exists: ${LUMINA_PEER}"
else
    fail "lumina.json missing: ${LUMINA_PEER} — run: skcapstone peer add lumina"
fi

# ─── 5. Daemon running (PID file) ─────────────────────────────────────────────

printf "\n${BOLD}5. SKChat daemon${RESET}\n"

PID_FILE="${SKCHAT_HOME}/daemon.pid"
if [[ -f "${PID_FILE}" ]]; then
    daemon_pid=$(cat "${PID_FILE}" 2>/dev/null || echo "")
    if [[ -n "${daemon_pid}" ]] && kill -0 "${daemon_pid}" 2>/dev/null; then
        pass "Daemon running (PID ${daemon_pid})"
    else
        fail "PID file exists but process dead (stale PID: ${daemon_pid:-?}) — run: skchat daemon start"
    fi
else
    fail "Daemon not running — no PID file at ${PID_FILE} — run: skchat daemon start"
fi

# ─── 6. Lumina bridge (systemd) ───────────────────────────────────────────────

printf "\n${BOLD}6. Lumina bridge (systemd)${RESET}\n"

if systemctl --user is-active --quiet skchat-lumina-bridge.service 2>/dev/null; then
    pass "skchat-lumina-bridge.service is active"
else
    # Fallback: check for process
    lumina_pid=$(pgrep -f "lumina-bridge.py" 2>/dev/null | head -1 || true)
    if [[ -n "${lumina_pid}" ]]; then
        pass "Lumina bridge process running (PID ${lumina_pid}, not via systemd)"
    else
        fail "Lumina bridge not running — run: systemctl --user start skchat-lumina-bridge"
    fi
fi

# ─── 7. MCP config has skchat entry ───────────────────────────────────────────

printf "\n${BOLD}7. MCP config (skchat entry)${RESET}\n"

if [[ -f "${MCP_CONFIG}" ]]; then
    if python3 -c "
import json, sys
d = json.load(open('${MCP_CONFIG}'))
mcp = d.get('mcpServers', d)  # support both top-level and nested
has_skchat = any(k.lower() in ('skchat', 'skchat-mcp') for k in mcp)
sys.exit(0 if has_skchat else 1)
" 2>/dev/null; then
        pass "MCP config ${MCP_CONFIG} has skchat entry"
    else
        fail "MCP config ${MCP_CONFIG} missing skchat entry — add skchat-mcp to mcpServers"
    fi
else
    fail "MCP config not found: ${MCP_CONFIG}"
fi

# ─── 8. skchat inbox --limit 1 ────────────────────────────────────────────────

printf "\n${BOLD}8. skchat inbox --limit 1${RESET}\n"

if command -v skchat &>/dev/null; then
    inbox_out=$(timeout 10 skchat inbox --limit 1 2>&1 || true)
    if [[ -n "${inbox_out}" ]]; then
        pass "skchat inbox responded"
        printf "     %s\n" "${inbox_out}" | head -3
    else
        fail "skchat inbox returned empty output (daemon may not be running)"
    fi
else
    fail "skchat binary unavailable — skipping inbox check"
fi

# ─── Summary ──────────────────────────────────────────────────────────────────

echo ""
printf "─────────────────────────────────────────────\n"
if [[ $FAIL -eq 0 ]]; then
    printf "${GREEN}${BOLD}All checks passed.${RESET}\n\n"
    exit 0
else
    printf "${RED}${BOLD}${FAIL} check(s) failed.${RESET}\n\n"
    exit 1
fi
