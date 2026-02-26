#!/usr/bin/env bash
# Launch SKChat on Linux with proper input method support.
# IBus socket can go stale between sessions — this ensures GTK can find it.

set -euo pipefail

BUNDLE_DIR="$(dirname "$0")/../build/linux/x64/release/bundle"

if [ ! -f "$BUNDLE_DIR/skchat" ]; then
  BUNDLE_DIR="$(dirname "$0")/../build/linux/x64/debug/bundle"
fi

if [ ! -f "$BUNDLE_DIR/skchat" ]; then
  echo "ERROR: No SKChat binary found. Run 'flutter build linux' first."
  exit 1
fi

# Ensure IBus daemon is alive and socket is fresh.
if command -v ibus-daemon &>/dev/null; then
  if ! ibus address &>/dev/null 2>&1; then
    ibus-daemon -drx 2>/dev/null
    sleep 1
  fi
  IBUS_ADDR="$(ibus address 2>/dev/null || true)"
  if [ -n "$IBUS_ADDR" ]; then
    export GTK_IM_MODULE=ibus
    export IBUS_ADDRESS="$IBUS_ADDR"
  fi
fi

exec "$BUNDLE_DIR/skchat" "$@"
