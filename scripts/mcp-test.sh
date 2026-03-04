#!/usr/bin/env bash
# mcp-test.sh — Verify skchat-mcp starts cleanly and all tools respond.
#
# Checks:
#   1. MCP server returns a valid initialize response
#   2. tools/list returns a non-empty tool list
#
# Output: PASS / FAIL lines with tool count.
# Exit code: 0 on full pass, 1 on any failure.

set -uo pipefail

IDENTITY="${SKCHAT_IDENTITY:-capauth:opus@skworld.io}"
SKCHAT_SRC="${SKCHAT_SRC:-/home/cbrd21/dkloud.douno.it/p/smilintux-org/skchat/src}"
TIMEOUT=30  # skchat-mcp startup takes ~16s on this machine (pyenv + heavy imports)

GREEN="\033[0;32m"
RED="\033[0;31m"
RESET="\033[0m"
BOLD="\033[1m"

FAIL=0

pass() { printf "  ${GREEN}PASS${RESET}  %s\n" "$1"; }
fail() { printf "  ${RED}FAIL${RESET}  %s\n" "$1"; FAIL=$((FAIL + 1)); }

echo ""
printf "${BOLD}SKChat MCP Server Test${RESET}\n"
printf "%s\n" "$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
printf "─────────────────────────────────────────\n"

# ─── Preflight ────────────────────────────────────────────────────

if ! command -v skchat-mcp &>/dev/null; then
    fail "skchat-mcp not in PATH — run: pip install -e '.[cli]'"
    echo ""
    exit 1
fi

if ! command -v python3 &>/dev/null; then
    fail "python3 not in PATH"
    echo ""
    exit 1
fi

# ─── Run two JSON-RPC messages, capture responses ─────────────────
# skchat-mcp buffers stdout; we need an active reader (head -N) so
# Python flushes its pipe buffer.  We capture to a temp file then
# read it back so we don't lose data after head closes the pipe.

TMPINPUT="$(mktemp)"
TMPOUT="$(mktemp)"
trap 'rm -f "$TMPINPUT" "$TMPOUT"' EXIT

cat > "$TMPINPUT" <<'JSONEOF'
{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"mcp-test","version":"1"}},"id":1}
{"jsonrpc":"2.0","method":"tools/list","params":{},"id":2}
JSONEOF

SKCHAT_IDENTITY="$IDENTITY" PYTHONPATH="$SKCHAT_SRC" \
    timeout "$TIMEOUT" skchat-mcp < "$TMPINPUT" 2>&1 \
    | head -5 > "$TMPOUT" 2>&1 || true

output="$(cat "$TMPOUT")"

# ─── 1. Initialize ────────────────────────────────────────────────

printf "\n${BOLD}1. MCP initialize${RESET}\n"

init_line=$(printf '%s\n' "$output" | grep '"id":1' | head -1)
if printf '%s' "$init_line" | python3 -c "
import sys, json
data = json.loads(sys.stdin.read())
assert 'result' in data, 'no result key'
assert 'serverInfo' in data['result'], 'no serverInfo'
name = data['result']['serverInfo']['name']
ver  = data['result']['serverInfo']['version']
print(f'server={name} version={ver}')
" 2>/dev/null; then
    info=$(printf '%s' "$init_line" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
r = d['result']
print(f\"{r['serverInfo']['name']} v{r['serverInfo']['version']}\")
")
    pass "Initialize OK — ${info}"
else
    fail "Initialize failed or malformed response"
    printf "     Raw: %s\n" "${init_line:-(empty)}"
fi

# ─── 2. tools/list ───────────────────────────────────────────────

printf "\n${BOLD}2. tools/list${RESET}\n"

tools_line=$(printf '%s\n' "$output" | grep '"id":2' | head -1)
if [[ -z "$tools_line" ]]; then
    fail "No tools/list response received"
else
    result=$(printf '%s' "$tools_line" | python3 -c "
import sys, json
data = json.loads(sys.stdin.read())
tools = data.get('result', {}).get('tools', [])
names = [t['name'] for t in tools]
print(len(tools))
for n in sorted(names):
    print(' ', n)
" 2>&1)

    if [[ $? -eq 0 ]]; then
        count=$(printf '%s\n' "$result" | head -1)
        tool_names=$(printf '%s\n' "$result" | tail -n +2)
        if [[ "$count" -gt 0 ]]; then
            pass "tools/list returned ${count} tools:"
            printf '%s\n' "$tool_names" | while read -r name; do
                printf "        • %s\n" "$name"
            done
        else
            fail "tools/list returned 0 tools"
        fi
    else
        fail "Could not parse tools/list response"
        printf "     Raw: %s\n" "${tools_line:0:200}"
    fi
fi

# ─── Summary ─────────────────────────────────────────────────────

echo ""
printf "─────────────────────────────────────────\n"
if [[ $FAIL -eq 0 ]]; then
    printf "${GREEN}${BOLD}PASS${RESET} — MCP server healthy\n"
else
    printf "${RED}${BOLD}FAIL${RESET} — %d check(s) failed\n" "$FAIL"
fi
echo ""

exit $FAIL
