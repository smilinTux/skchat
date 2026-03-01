#!/usr/bin/env bash
# Launch SKChat on Linux with proper input method support.
# IBus socket can go stale between sessions — this ensures GTK can find it.
#
# Usage:
#   ./launch-linux.sh              # Run existing build (release > debug)
#   ./launch-linux.sh --build      # Build release first, then run
#   ./launch-linux.sh --debug      # Build debug first, then run
#   ./launch-linux.sh --run        # Use 'flutter run -d linux' (hot reload)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ARCH="$(uname -m)"

# Map uname arch to Flutter's directory naming
case "$ARCH" in
  x86_64)  FLUTTER_ARCH="x64" ;;
  aarch64) FLUTTER_ARCH="arm64" ;;
  *)       FLUTTER_ARCH="$ARCH" ;;
esac

RELEASE_BUNDLE="$PROJECT_DIR/build/linux/$FLUTTER_ARCH/release/bundle"
DEBUG_BUNDLE="$PROJECT_DIR/build/linux/$FLUTTER_ARCH/debug/bundle"

# Handle flags
BUILD_MODE=""
for arg in "$@"; do
  case "$arg" in
    --build)   BUILD_MODE="release"; shift ;;
    --debug)   BUILD_MODE="debug"; shift ;;
    --run)     BUILD_MODE="run"; shift ;;
    --help|-h)
      echo "Usage: $(basename "$0") [--build|--debug|--run] [-- flutter-args...]"
      echo "  (no flag)   Run existing build (release preferred over debug)"
      echo "  --build     Build release, then run"
      echo "  --debug     Build debug, then run"
      echo "  --run       Use 'flutter run -d linux' (supports hot reload)"
      exit 0
      ;;
  esac
done

# If --run, delegate to flutter run and exit
if [ "$BUILD_MODE" = "run" ]; then
  cd "$PROJECT_DIR"
  echo "Starting SKChat via flutter run (hot reload enabled)..."
  exec flutter run -d linux "$@"
fi

# If --build or --debug, build first
if [ "$BUILD_MODE" = "release" ]; then
  echo "Building SKChat (release)..."
  cd "$PROJECT_DIR"
  flutter build linux --release
elif [ "$BUILD_MODE" = "debug" ]; then
  echo "Building SKChat (debug)..."
  cd "$PROJECT_DIR"
  flutter build linux --debug
fi

# Find the binary (prefer release over debug)
BUNDLE_DIR=""
if [ -f "$RELEASE_BUNDLE/skchat" ]; then
  BUNDLE_DIR="$RELEASE_BUNDLE"
elif [ -f "$DEBUG_BUNDLE/skchat" ]; then
  BUNDLE_DIR="$DEBUG_BUNDLE"
fi

if [ -z "$BUNDLE_DIR" ]; then
  echo "No SKChat binary found at:"
  echo "  $RELEASE_BUNDLE/skchat"
  echo "  $DEBUG_BUNDLE/skchat"
  echo ""
  echo "Quick start options:"
  echo "  $(basename "$0") --build    # Build release and run"
  echo "  $(basename "$0") --debug    # Build debug and run"
  echo "  $(basename "$0") --run      # flutter run with hot reload"
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
