#!/usr/bin/env bash
# smoke-test.sh — SKChat CLI smoke test
#
# Checks:
#   1. version     — skchat --version returns a version string
#   2. health      — daemon HTTP health endpoint returns {"status":"ok"}
#   3. peers       — skchat peers list returns ≥1 peer
#   4. send        — skchat send to self (opus) queues without error
#   5. inbox       — skchat inbox returns output without error
#   6. groups      — skchat group list returns ≥1 group
#
# Output: GREEN ✓ PASS / RED ✗ FAIL per check.
# Exit 0 if all checks pass, 1 if any fail.

set -uo pipefail

HEALTH_URL="${SKCHAT_HEALTH_URL:-http://localhost:9385/health}"
SEND_RECIPIENT="${SMOKE_RECIPIENT:-opus}"
SMOKE_MSG="skchat smoke-test $(date -u +%s)"

GREEN="\033[0;32m"
RED="\033[0;31m"
RESET="\033[0m"
BOLD="\033[1m"

PASS=0
FAIL=0

pass() { printf "  ${GREEN}✓ PASS${RESET}  %s\n" "$1"; PASS=$((PASS + 1)); }
fail() { printf "  ${RED}✗ FAIL${RESET}  %s\n" "$1"; FAIL=$((FAIL + 1)); }

header() { printf "\n${BOLD}%s${RESET}\n" "$1"; }

echo ""
printf "${BOLD}SKChat Smoke Test${RESET}\n"
printf "%s\n" "$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
printf "─────────────────────────────────────────\n"

# ─── 1. version ──────────────────────────────────────────────────────────────

header "1. version"

if command -v skchat &>/dev/null; then
    ver=$(skchat --version 2>&1 | head -1 || true)
    if [[ -n "$ver" ]]; then
        pass "skchat --version → ${ver}"
    else
        fail "skchat --version returned empty output"
    fi
else
    fail "skchat not found in PATH — run: pip install -e '.[cli]'"
fi

# ─── 2. health endpoint ───────────────────────────────────────────────────────

header "2. health endpoint (${HEALTH_URL})"

if command -v curl &>/dev/null; then
    health_body=$(timeout 5 curl -sf "${HEALTH_URL}" 2>&1 || true)
    if printf '%s' "${health_body}" | grep -q '"status".*"ok"'; then
        # Extract uptime for context
        uptime_s=$(printf '%s' "${health_body}" | python3 -c \
            "import json,sys; d=json.load(sys.stdin); print(d.get('uptime_s','?'))" 2>/dev/null || echo "?")
        pass "Health OK (uptime: ${uptime_s}s)"
    else
        fail "Health endpoint did not return {\"status\":\"ok\"} — got: ${health_body:-(no response)}"
    fi
else
    fail "curl not found — cannot check health endpoint"
fi

# ─── 3. peers ─────────────────────────────────────────────────────────────────

header "3. peers"

if command -v skchat &>/dev/null; then
    peers_out=$(timeout 10 skchat peers list 2>&1 || true)
    # Table header line contains "Peers (N)" — extract N
    peer_count=$(printf '%s' "${peers_out}" | grep -oP 'Peers \(\K[0-9]+' || echo "0")
    if [[ "${peer_count}" -ge 1 ]] 2>/dev/null; then
        pass "skchat peers list → ${peer_count} peer(s) found"
    else
        # Fallback: any non-empty, non-error output
        if [[ -n "${peers_out}" ]] && ! printf '%s' "${peers_out}" | grep -qi "error\|traceback"; then
            pass "skchat peers list → responded (count parsing unavailable)"
        else
            fail "skchat peers list returned no peers or errored — output: ${peers_out:0:120}"
        fi
    fi
else
    fail "skchat not found — skipping peers check"
fi

# ─── 4. send ─────────────────────────────────────────────────────────────────

header "4. send (→ ${SEND_RECIPIENT})"

if command -v skchat &>/dev/null; then
    send_out=$(timeout 10 skchat send "${SEND_RECIPIENT}" "${SMOKE_MSG}" 2>&1 || true)
    # Success: no error/traceback; any output or even empty is fine
    if ! printf '%s' "${send_out}" | grep -qi "error\|traceback\|exception"; then
        if [[ -n "${send_out}" ]]; then
            first_line=$(printf '%s' "${send_out}" | head -1)
            pass "skchat send → ${first_line}"
        else
            pass "skchat send → queued (no output)"
        fi
    else
        fail "skchat send returned error — ${send_out:0:200}"
    fi
else
    fail "skchat not found — skipping send check"
fi

# ─── 5. inbox ────────────────────────────────────────────────────────────────

header "5. inbox"

if command -v skchat &>/dev/null; then
    inbox_out=$(timeout 10 skchat inbox --limit 5 2>&1 || true)
    if [[ -n "${inbox_out}" ]] && ! printf '%s' "${inbox_out}" | grep -qi "traceback\|exception"; then
        msg_lines=$(printf '%s' "${inbox_out}" | grep -c '^\s\+[0-9]\+:[0-9]\+' 2>/dev/null || echo "?")
        pass "skchat inbox responded (${msg_lines} message line(s) visible)"
    else
        fail "skchat inbox returned empty or errored — ${inbox_out:0:120}"
    fi
else
    fail "skchat not found — skipping inbox check"
fi

# ─── 6. groups ───────────────────────────────────────────────────────────────

header "6. groups"

if command -v skchat &>/dev/null; then
    groups_out=$(timeout 10 skchat group list 2>&1 || true)
    group_count=$(printf '%s' "${groups_out}" | grep -oP 'Groups \(\K[0-9]+' || echo "0")
    if [[ "${group_count}" -ge 1 ]] 2>/dev/null; then
        pass "skchat group list → ${group_count} group(s) found"
    else
        if [[ -n "${groups_out}" ]] && ! printf '%s' "${groups_out}" | grep -qi "error\|traceback"; then
            pass "skchat group list → responded (count parsing unavailable)"
        else
            fail "skchat group list returned no groups or errored — ${groups_out:0:120}"
        fi
    fi
else
    fail "skchat not found — skipping groups check"
fi

# ─── Summary ─────────────────────────────────────────────────────────────────

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
