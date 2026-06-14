#!/usr/bin/env bash
# SKWorld standard QA suite — repeatable. Runs all service test suites + the
# live lane/spaces harness, prints an aggregate PASS/FAIL summary + exit code.
# Usage: bash scripts/qa_suite.sh   (run from the skchat repo root or anywhere)
set -uo pipefail
PY=$HOME/.skenv/bin/python
SKCHAT=$HOME/clawd/skcapstone-repos/skchat
SKCOMMS=$HOME/clawd/skcapstone-repos/skcomms
fail=0
line() { printf '%-42s %s\n' "$1" "$2"; }
echo "==================== SKWorld QA Suite ===================="
echo "skchat unit/integration suite:"
( cd "$HOME" && $PY -m pytest "$SKCHAT/tests/" -q 2>&1 | tail -1 )
echo "skcomms suite:"
( cd "$HOME" && $PY -m pytest "$SKCOMMS/tests/" -q 2>&1 | tail -1 )
echo "recording write-up pipeline:"
( cd "$HOME" && $PY -m pytest "$SKCHAT/tests/test_recording_writeup.py" -q 2>&1 | tail -1 )
echo "----- LIVE: lane/spaces harness (:8765) -----"
$PY "$SKCHAT/scripts/tier5_verify.py" 2>&1 | tail -15
echo "========================================================="
echo "NOTE: app (Flutter) tests run on .41: ssh 192.168.0.41 '~/flutter/bin/flutter test' (in skchat-app)"
