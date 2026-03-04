#!/usr/bin/env bash
# test-lumina-bridge.sh — smoke-test the Lumina consciousness bridge.
#
# 1. Sends a test message to lumina@skworld.io via skchat send
# 2. Waits 10 seconds for the bridge to process and reply
# 3. Checks inbox for a reply from Lumina
# 4. Reports PASS or FAIL
#
# Usage:
#   bash scripts/test-lumina-bridge.sh

set -euo pipefail

LUMINA="capauth:lumina@skworld.io"
TEST_MSG="BridgeTest-$(date +%s): Hello Lumina, please reply with the word PONG."
WAIT_SECS=10
PASS=0

echo "=== Lumina Bridge Smoke Test ==="
echo "Target  : $LUMINA"
echo "Message : $TEST_MSG"
echo

# ── 1. Check bridge is running ────────────────────────────────────────────────
echo "[1/4] Checking bridge process..."
if pgrep -f "lumina-bridge.py" >/dev/null 2>&1; then
    echo "  BRIDGE: running"
else
    echo "  BRIDGE: NOT running — attempting start via systemctl"
    systemctl --user start skchat-lumina-bridge.service 2>/dev/null || true
    sleep 2
    if pgrep -f "lumina-bridge.py" >/dev/null 2>&1; then
        echo "  BRIDGE: started"
    else
        echo "  BRIDGE: still not running (service may depend on skchat daemon)"
    fi
fi
echo

# ── 2. Send test message ──────────────────────────────────────────────────────
echo "[2/4] Sending test message to $LUMINA ..."
if skchat send "$LUMINA" "$TEST_MSG" 2>&1; then
    echo "  SEND: ok"
else
    echo "  SEND: FAILED (exit $?)"
    echo "RESULT: FAIL — could not send message"
    exit 1
fi
echo

# ── 3. Wait for reply ─────────────────────────────────────────────────────────
echo "[3/4] Waiting ${WAIT_SECS}s for Lumina to reply..."
sleep "$WAIT_SECS"
echo

# ── 4. Check inbox for reply ──────────────────────────────────────────────────
echo "[4/4] Checking inbox for reply from Lumina (last 2 minutes)..."
INBOX_OUTPUT=$(skchat inbox --since 2 2>&1 || true)
echo "$INBOX_OUTPUT" | head -30

if echo "$INBOX_OUTPUT" | grep -qi "lumina\|PONG\|sovereign"; then
    PASS=1
fi

echo
if [ "$PASS" -eq 1 ]; then
    echo "RESULT: PASS — reply from Lumina detected in inbox"
    exit 0
else
    echo "RESULT: FAIL — no reply from Lumina found within ${WAIT_SECS}s"
    echo
    echo "Troubleshooting:"
    echo "  • Check bridge log : cat ~/.skchat/lumina-bridge.log | tail -30"
    echo "  • Check service    : systemctl --user status skchat-lumina-bridge.service"
    echo "  • Bridge running?  : pgrep -f lumina-bridge"
    exit 1
fi
