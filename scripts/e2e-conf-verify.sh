#!/usr/bin/env bash
# E2E verification script for the Sovereign Conf Calls pipeline.
#
# Tests the full lifecycle through both tailnet and Funnel ingress:
#   create → token → invite → guest join → ICE → health → end
#
# Usage:
#   bash scripts/e2e-conf-verify.sh
#
# Exit code: 0 = all phases pass, 1 = any phase failed.
# Prints PASS/FAIL for each phase.

set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
SKCHAT=${SKCHAT_URL:-http://localhost:8765}
FUNNEL=${FUNNEL_URL:-https://noroc2027.tail204f0c.ts.net:10000}
PASS=0
FAIL=0

pass() { echo "  ✅ PASS: $1"; ((PASS++)); }
fail() { echo "  ❌ FAIL: $1"; ((FAIL++)); }

echo "═══════════════════════════════════════════════════════════════"
echo "  E2E Conf Verification — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "═══════════════════════════════════════════════════════════════"
echo "  SKChat API:  $SKCHAT"
echo "  Funnel URL:  $FUNNEL"
echo ""

# ── Phase 1: Conf Creation ────────────────────────────────────────
echo "── Phase 1: Conf creation ──"
CONF=$(curl -sf "$SKCHAT/conf/create" -X POST \
  -H 'Content-Type: application/json' \
  -d '{"host_fqid":"lumina@chef.skworld","title":"E2E Verify","slug":"e2e-verify"}') || { fail "create conf"; ROOM=""; }
if [ -n "$CONF" ]; then
  ROOM=$(echo "$CONF" | python3 -c "import sys,json; print(json.load(sys.stdin)['room'])" 2>/dev/null) || ROOM=""
  CONF_ID=$(echo "$CONF" | python3 -c "import sys,json; print(json.load(sys.stdin)['conf_id'])" 2>/dev/null) || CONF_ID=""
  if [ -n "$ROOM" ]; then
    pass "created conf $CONF_ID (room: $ROOM)"
  else
    fail "create conf — no room in response"
  fi
fi

# ── Phase 2: Conf Token ────────────────────────────────────────────
echo "── Phase 2: Token minting ──"
if [ -n "$ROOM" ]; then
  TOKEN_R=$(curl -sf "$SKCHAT/conf/$ROOM/token" -X POST \
    -H 'Content-Type: application/json' \
    -d '{"identity":"chef@chef.skworld","name":"Chef"}') && pass "minted conf token" || fail "mint conf token"

  # Verify token is a valid JWT
  if [ -n "$TOKEN_R" ]; then
    JWT=$(echo "$TOKEN_R" | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])" 2>/dev/null) || JWT=""
    if [ -n "$JWT" ]; then
      pass "minted conf token"
    else
      fail "mint conf token — no token in response"
    fi
  fi
fi

# ── Phase 3: Guest Invite ──────────────────────────────────────────
echo "── Phase 3: Guest invite ──"
if [ -n "$ROOM" ]; then
  INVITE=$(curl -sf "$SKCHAT/guest/invite" -X POST \
    -H 'Content-Type: application/json' \
    -d "{\"room\":\"$ROOM\",\"display\":\"Alice\",\"single_use\":true}") && pass "created invite" || fail "create invite"

  INVITE_TOKEN=$(echo "$INVITE" | python3 -c "import sys,json; print(json.load(sys.stdin)['invite_token'])" 2>/dev/null) || INVITE_TOKEN=""
  INVITE_URL=$(echo "$INVITE" | python3 -c "import sys,json; print(json.load(sys.stdin)['invite_url'])" 2>/dev/null) || INVITE_URL=""

  # Verify invite URL points to Funnel
  if echo "$INVITE_URL" | grep -q "$FUNNEL"; then
    pass "invite URL points to Funnel"
  else
    fail "invite URL does not point to Funnel: $INVITE_URL"
  fi
fi

# ── Phase 4: Guest Join via Funnel ─────────────────────────────────
echo "── Phase 4: Guest join via Funnel ──"
if [ -n "$ROOM" ] && [ -n "$INVITE_TOKEN" ]; then
  # Join page reachable
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$FUNNEL/join/$ROOM?invite=$INVITE_TOKEN" 2>/dev/null)
  if [ "$STATUS" = "200" ]; then
    pass "join page reachable via Funnel (HTTP $STATUS)"
  else
    fail "join page returned HTTP $STATUS"
  fi

  # Guest join
  GUEST_JOIN=$(curl -sf "$FUNNEL/guest/join" -X POST \
    -H 'Content-Type: application/json' \
    -d "{\"room\":\"$ROOM\",\"invite_token\":\"$INVITE_TOKEN\",\"display_name\":\"Alice\"}") && pass "guest join via Funnel" || fail "guest join via Funnel"

  GUEST_IDENTITY=$(echo "$GUEST_JOIN" | python3 -c "import sys,json; print(json.load(sys.stdin)['identity'])" 2>/dev/null)
  GUEST_URL=$(echo "$GUEST_JOIN" | python3 -c "import sys,json; print(json.load(sys.stdin)['lk_url'])" 2>/dev/null)

  # Verify guest identity format
  if echo "$GUEST_IDENTITY" | grep -q "^guest:"; then
    pass "guest identity format correct: $GUEST_IDENTITY"
  else
    fail "guest identity unexpected: $GUEST_IDENTITY"
  fi

  # Verify lk_url points to SFU
  if echo "$GUEST_URL" | grep -q "wss://"; then
    pass "guest gets wss SFU URL"
  else
    fail "guest missing wss SFU URL"
  fi

  # Verify LiveKit token in response
  GUEST_LK_TOKEN=$(echo "$GUEST_JOIN" | python3 -c "import sys,json; print(json.load(sys.stdin).get('lk_token',''))" 2>/dev/null)
  if [ -n "$GUEST_LK_TOKEN" ]; then
    pass "guest received LiveKit token"
  else
    fail "guest missing LiveKit token"
  fi
fi

# ── Phase 5: ICE Config ────────────────────────────────────────────
echo "── Phase 5: ICE config ──"
ICE=$(python3 -c "
import os
secret_file = os.path.expanduser('~/.skchat/coturn/coturn.secret')
if os.path.exists(secret_file):
    os.environ['SKCHAT_TURN_SECRET'] = open(secret_file).read().strip()
os.environ['SKCHAT_TURN_URLS'] = 'turn:noroc2027.tail204f0c.ts.net:3478'
from skchat.connectivity import ice_config
cfg = ice_config('lumina@chef.skworld', 'public@guest', {'on_tailnet': False})
servers = cfg['ice_servers']
print(f'servers={len(servers)}')
for s in servers:
    urls = ' '.join(s.get('urls', []))
    if 'stun' in urls: print(f'stun=found')
    if 'turn' in urls: print(f'turn=found')
print(f'tier={cfg[\"preferred_tier\"]}')
" 2>/dev/null) || ICE=""

if echo "$ICE" | grep -q "servers=2" && echo "$ICE" | grep -q "stun=found" && echo "$ICE" | grep -q "turn=found"; then
  pass "ICE config has STUN + sovereign TURN"
else
  fail "ICE config missing expected servers: $ICE"
fi

# ── Phase 6: Conf Health ───────────────────────────────────────────
echo "── Phase 6: Conf health ──"
HEALTH=$(curl -sf "$SKCHAT/conf/health") && pass "conf health endpoint OK" || fail "conf health endpoint"
if [ -n "$HEALTH" ]; then
  LIVE_CONFS=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('live_confs',0))" 2>/dev/null)
  LK_OK=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('livekit_configured',False))" 2>/dev/null)
  if [ "$LIVE_CONFS" -ge 1 ]; then
    pass "health reports $LIVE_CONFS live conf(s)"
  fi
  if [ "$LK_OK" = "True" ]; then
    pass "health confirms LiveKit configured"
  fi
fi

# ── Phase 7: Admin Routes Tailnet-Only ──────────────────────────────
echo "── Phase 7: Admin route isolation ──"
for route in /guest/invite /pair/scan /spaces; do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$FUNNEL$route" 2>/dev/null)
  if [ "$STATUS" = "404" ]; then
    pass "admin route $route blocked on Funnel (HTTP 404)"
  else
    fail "admin route $route exposed on Funnel (HTTP $STATUS)"
  fi
done

# ── Phase 8: Conf List ──────────────────────────────────────────────
echo "── Phase 8: Conf listing ──"
CONFS=$(curl -sf "$SKCHAT/conf") && pass "list confs endpoint OK" || fail "list confs"
LIVE_COUNT=$(echo "$CONFS" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('confs',[])))" 2>/dev/null)
echo "  (live confs: $LIVE_COUNT — lingering from this test if end skipped)"

# ── Phase 9: End Conf ──────────────────────────────────────────────
echo "── Phase 9: End conf ──"
if [ -n "$ROOM" ]; then
  END_R=$(curl -sf "$SKCHAT/conf/$ROOM/end" -X POST \
    -H 'Content-Type: application/json' \
    -d '{"requester":"lumina@chef.skworld"}') && pass "ended conf" || fail "end conf"
  if echo "$END_R" | python3 -c "import sys,json; sys.exit(0 if json.load(sys.stdin).get('ok') else 1)" 2>/dev/null; then
    pass "end conf returned ok=true"
  fi
fi

# ── Summary ─────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Results: $PASS passed, $FAIL failed"
echo "═══════════════════════════════════════════════════════════════"
if [ "$FAIL" -gt 0 ]; then exit 1; fi
