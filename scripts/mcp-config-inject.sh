#!/usr/bin/env bash
# mcp-config-inject.sh -- add skchat to ~/.claude/settings.json
# Usage: ./mcp-config-inject.sh [--settings PATH] [--dry-run]
# Requires: jq
set -euo pipefail

SETTINGS="${SETTINGS_PATH:-${HOME}/.claude/settings.json}"
DRY_RUN=false
SKCHAT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.."; pwd)"
SKCHAT_SRC="${SKCHAT_DIR}/src"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --settings) SETTINGS="$2"; shift 2 ;;
    --dry-run)  DRY_RUN=true; shift ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if ! command -v jq &>/dev/null; then
  echo "Error: jq required. Install: sudo pacman -S jq" >&2
  exit 1
fi

if [[ ! -f "$SETTINGS" ]]; then
  echo "{}" > "$SETTINGS"
  echo "Created $SETTINGS"
fi

IDENTITY="${SKCHAT_IDENTITY:-capauth:opus@skworld.io}"

ENTRY=$(jq -n \
  --arg cmd "python3" \
  --argjson args '["-m","skchat.mcp_server"]' \
  --arg cwd "$SKCHAT_DIR" \
  --arg pypath "$SKCHAT_SRC" \
  --arg identity "$IDENTITY" \
  '{ command:$cmd, args:$args, cwd:$cwd, env:{PYTHONPATH:$pypath, SKCHAT_IDENTITY:$identity} }')

UPDATED=$(jq --argjson e "$ENTRY" '.mcpServers.skchat=$e' "$SETTINGS")

if $DRY_RUN; then
  echo "$UPDATED"
else
  TMP=$(mktemp "${SETTINGS}.XXXXXX")
  echo "$UPDATED" > "$TMP"
  mv "$TMP" "$SETTINGS"
  echo "Updated $SETTINGS -- skchat configured."
fi
